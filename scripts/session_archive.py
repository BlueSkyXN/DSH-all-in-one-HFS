#!/usr/bin/env python3
"""Append-only archive for the container-local DSH JSONL session store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = 1
CHUNK_SIZE = 1024 * 1024
MANIFEST_DIGITS = 20


class ArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileState:
    path: PurePosixPath
    size: int
    mode: int
    mtime_ns: int
    inode: int


@dataclass(frozen=True)
class Chunk:
    object_name: PurePosixPath
    offset: int
    size: int
    sha256: str


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def safe_relative(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ArchiveError(f"unsafe archive path: {path!r}")
    return candidate


def object_path(value: str) -> PurePosixPath:
    candidate = safe_relative(value)
    if len(candidate.parts) != 2 or candidate.parts[0] != "chunks":
        raise ArchiveError(f"unsafe chunk object: {value!r}")
    return candidate


def sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK_SIZE)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def ensure_archive_dirs(archive: Path) -> None:
    archive.mkdir(mode=0o700, parents=True, exist_ok=True)
    (archive / "manifests").mkdir(mode=0o700, exist_ok=True)
    (archive / "chunks").mkdir(mode=0o700, exist_ok=True)
    for directory in (archive, archive / "manifests", archive / "chunks"):
        metadata = directory.lstat()
        if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ArchiveError(f"archive path is not a real directory: {directory}")


def scan_source(source: Path) -> dict[str, FileState]:
    if not source.exists():
        return {}
    if not source.is_dir() or source.is_symlink():
        raise ArchiveError(f"session source is not a directory: {source}")

    files: dict[str, FileState] = {}
    for current_root, directory_names, file_names in os.walk(source, followlinks=False):
        root_path = Path(current_root)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            path = root_path / name
            if path.is_symlink():
                raise ArchiveError(f"symlink is unsupported in the session store: {path}")
            kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in sorted(file_names):
            path = root_path / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ArchiveError(f"non-regular file in the session store: {path}")
            relative = safe_relative(path.relative_to(source).as_posix())
            files[relative.as_posix()] = FileState(
                path=relative,
                size=metadata.st_size,
                mode=stat.S_IMODE(metadata.st_mode),
                mtime_ns=metadata.st_mtime_ns,
                inode=metadata.st_ino,
            )
    return files


def manifest_paths(archive: Path) -> list[Path]:
    directory = archive / "manifests"
    if not directory.exists():
        return []
    paths: list[Path] = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        try:
            serial = int(path.name.removesuffix(".json"), 10)
        except ValueError:
            continue
        if path.name != f"{serial:0{MANIFEST_DIGITS}d}.json":
            continue
        paths.append(path)
    return sorted(paths, key=lambda path: path.name)


def parse_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArchiveError(f"invalid manifest {path.name}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != SCHEMA_VERSION:
        raise ArchiveError(f"unsupported manifest schema in {path.name}")
    if not isinstance(value.get("serial"), int) or value["serial"] < 1:
        raise ArchiveError(f"invalid manifest serial in {path.name}")
    if not isinstance(value.get("files"), list):
        raise ArchiveError(f"invalid file list in {path.name}")
    return value


def validate_manifest_layout(manifest: dict[str, Any]) -> None:
    seen_paths: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise ArchiveError("manifest file entry is not an object")
        relative = safe_relative(str(entry.get("path", "")))
        if relative.as_posix() in seen_paths:
            raise ArchiveError(f"duplicate manifest path: {relative}")
        seen_paths.add(relative.as_posix())
        for key in ("size", "mode", "mtimeNs"):
            if not isinstance(entry.get(key), int) or entry[key] < 0:
                raise ArchiveError(f"invalid {key} for {relative}")
        chunks = entry.get("chunks")
        if not isinstance(chunks, list):
            raise ArchiveError(f"invalid chunks for {relative}")
        expected_offset = 0
        total_size = 0
        for chunk in chunks:
            if not isinstance(chunk, dict):
                raise ArchiveError(f"invalid chunk for {relative}")
            object_path(str(chunk.get("object", "")))
            offset = chunk.get("offset")
            size = chunk.get("size")
            digest = chunk.get("sha256")
            if offset != expected_offset:
                raise ArchiveError(f"non-contiguous chunks for {relative}")
            if not isinstance(size, int) or size < 0:
                raise ArchiveError(f"invalid chunk size for {relative}")
            if not isinstance(digest, str) or len(digest) != 64:
                raise ArchiveError(f"invalid chunk digest for {relative}")
            expected_offset += size
            total_size += size
        if total_size != entry["size"]:
            raise ArchiveError(f"chunk total does not match size for {relative}")


def verify_manifest_chunks(archive: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["files"]:
        for chunk in entry["chunks"]:
            object_name = object_path(chunk["object"])
            path = archive / object_name
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise ArchiveError(f"archive object is not a regular file: {object_name}")
            if metadata.st_size != chunk["size"]:
                raise ArchiveError(f"archive object size mismatch: {object_name}")
            _, digest = sha256_file(path)
            if digest != chunk["sha256"]:
                raise ArchiveError(f"archive object digest mismatch: {object_name}")


def load_latest_manifest(
    archive: Path,
    *,
    verify_chunks: bool,
) -> tuple[dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    found = False
    for path in reversed(manifest_paths(archive)):
        found = True
        try:
            manifest = parse_manifest(path)
            validate_manifest_layout(manifest)
            if verify_chunks:
                verify_manifest_chunks(archive, manifest)
            return manifest, problems
        except ArchiveError as error:
            problems.append(str(error))
    if found:
        raise ArchiveError(f"no valid session archive manifest; problems: {problems}")
    return None, problems


def write_chunk(
    archive: Path,
    source_file: Path,
    offset: int,
    length: int,
) -> Chunk | None:
    if length == 0:
        return None
    object_name = PurePosixPath("chunks") / f"{uuid.uuid4()}.bin"
    destination = archive / object_name
    digest = hashlib.sha256()
    copied = 0
    try:
        with source_file.open("rb") as source, destination.open("xb") as target:
            source.seek(offset)
            while copied < length:
                block = source.read(min(CHUNK_SIZE, length - copied))
                if not block:
                    raise ArchiveError(f"session file changed while archiving: {source_file}")
                target.write(block)
                digest.update(block)
                copied += len(block)
            target.flush()
        os.chmod(destination, 0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if copied != length:
        destination.unlink(missing_ok=True)
        raise ArchiveError(f"short read while archiving: {source_file}")
    return Chunk(
        object_name=object_name,
        offset=offset,
        size=copied,
        sha256=digest.hexdigest(),
    )


def previous_file_entries(manifest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if manifest is None:
        return {}
    return {str(entry["path"]): entry for entry in manifest["files"]}


def manifest_matches_current(
    current_files: dict[str, FileState],
    previous: dict[str, dict[str, Any]],
) -> bool:
    if set(current_files) != set(previous):
        return False
    for relative, state in current_files.items():
        old = previous[relative]
        if (
            state.size != old["size"]
            or state.mode != old["mode"]
            or state.mtime_ns != old["mtimeNs"]
            or state.inode != old.get("sourceInode")
        ):
            return False
    return True


def copy_source_chunks(
    archive: Path,
    source: Path,
    current_files: dict[str, FileState],
    previous: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for relative, state in sorted(current_files.items()):
        old = previous.get(relative)
        chunks: list[dict[str, Any]]
        unchanged = (
            old is not None
            and state.size == old["size"]
            and state.mtime_ns == old["mtimeNs"]
        )
        append_tail = (
            old is not None
            and state.size > old["size"]
            and state.inode == old.get("sourceInode")
        )
        if old is not None and unchanged:
            chunks = list(old["chunks"])
        elif old is not None and append_tail:
            chunks = list(old["chunks"])
            new_chunk = write_chunk(
                archive,
                source / state.path,
                old["size"],
                state.size - old["size"],
            )
            if new_chunk is not None:
                chunks.append(
                    {
                        "object": new_chunk.object_name.as_posix(),
                        "offset": new_chunk.offset,
                        "size": new_chunk.size,
                        "sha256": new_chunk.sha256,
                    }
                )
        else:
            chunks = []
            full_copy = write_chunk(archive, source / state.path, 0, state.size)
            if full_copy is not None:
                chunks.append(
                    {
                        "object": full_copy.object_name.as_posix(),
                        "offset": 0,
                        "size": full_copy.size,
                        "sha256": full_copy.sha256,
                    }
                )
        entries.append(
            {
                "path": relative,
                "size": state.size,
                "mode": state.mode,
                "mtimeNs": state.mtime_ns,
                "sourceInode": state.inode,
                "chunks": chunks,
            }
        )
    return entries


def ensure_objects_available(archive: Path, manifest: dict[str, Any] | None) -> None:
    if manifest is None:
        return
    for entry in manifest["files"]:
        for chunk in entry["chunks"]:
            object_name = object_path(chunk["object"])
            path = archive / object_name
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise ArchiveError(f"missing archive object: {object_name}")
            if metadata.st_size != chunk["size"]:
                raise ArchiveError(f"archive object size mismatch: {object_name}")


def next_serial(archive: Path) -> int:
    serial = 0
    for path in manifest_paths(archive):
        try:
            serial = max(serial, int(path.name.removesuffix(".json"), 10))
        except ValueError:
            continue
    return serial + 1


def write_manifest(archive: Path, entries: list[dict[str, Any]]) -> int:
    serial = next_serial(archive)
    manifest = {
        "schema": SCHEMA_VERSION,
        "serial": serial,
        "createdAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dshVersion": os.environ.get("DSH_VERSION", ""),
        "files": entries,
    }
    destination = archive / "manifests" / f"{serial:0{MANIFEST_DIGITS}d}.json"
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
    os.chmod(destination, 0o600)
    return serial


def backup_once(source: Path, archive: Path) -> int:
    ensure_archive_dirs(archive)
    latest, _ = load_latest_manifest(archive, verify_chunks=False)
    ensure_objects_available(archive, latest)
    current_files = scan_source(source)
    previous = previous_file_entries(latest)
    if not current_files and latest is not None and latest["files"]:
        raise ArchiveError("session source became empty; refusing to overwrite the archive")
    if current_files and any(path not in current_files for path in previous):
        raise ArchiveError("session source is missing files present in the latest archive")

    if manifest_matches_current(current_files, previous):
        return latest["serial"] if latest is not None else 0
    entries = copy_source_chunks(archive, source, current_files, previous)
    return write_manifest(archive, entries)


def restore_archive(archive: Path, destination: Path) -> None:
    latest, _ = load_latest_manifest(archive, verify_chunks=True)
    runtime_version = os.environ.get("DSH_VERSION", "")
    if latest is not None and runtime_version != latest.get("dshVersion", ""):
        raise ArchiveError(
            f"archive DSH version mismatch: archive={latest.get('dshVersion')!r} "
            f"runtime={runtime_version!r}"
        )

    parent = destination.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=parent))
    previous_destination: Path | None = None
    try:
        if latest is not None:
            for entry in latest["files"]:
                relative = safe_relative(entry["path"])
                target = temporary / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                copied = 0
                with target.open("xb") as output:
                    for chunk in entry["chunks"]:
                        object_name = object_path(chunk["object"])
                        chunk_path = archive / object_name
                        with chunk_path.open("rb") as input_handle:
                            while True:
                                block = input_handle.read(CHUNK_SIZE)
                                if not block:
                                    break
                                output.write(block)
                                copied += len(block)
                if copied != entry["size"]:
                    raise ArchiveError(f"restored size mismatch: {relative}")
                os.chmod(target, entry["mode"])
                os.utime(target, ns=(entry["mtimeNs"], entry["mtimeNs"]))

        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise ArchiveError(f"restore destination is not a directory: {destination}")
            previous_destination = parent / f".{destination.name}.previous-{uuid.uuid4()}"
            os.rename(destination, previous_destination)
        os.rename(temporary, destination)
        temporary = None  # type: ignore[assignment]
        if previous_destination is not None:
            shutil.rmtree(previous_destination)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def watch(source: Path, archive: Path, interval: int, max_failures: int) -> None:
    stop_event = threading.Event()

    def request_stop(signal_number: int, frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    failures = 0
    while not stop_event.is_set():
        if stop_event.wait(interval):
            break
        try:
            backup_once(source, archive)
            failures = 0
        except Exception as error:
            failures += 1
            limit = "unlimited" if max_failures == 0 else str(max_failures)
            print(
                f"[dsh-hfs] session archive error ({failures}/{limit}): {error}",
                file=sys.stderr,
                flush=True,
            )
            if max_failures > 0 and failures >= max_failures:
                raise ArchiveError("session archive failure limit reached") from error


def tree_hash(root: Path) -> dict[str, tuple[int, bytes, int, int]]:
    result: dict[str, tuple[int, bytes, int, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        metadata = path.stat()
        data = path.read_bytes()
        result[relative] = (
            len(data),
            hashlib.sha256(data).digest(),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_mtime_ns,
        )
    return result


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-session-archive-test-") as temporary_name:
        temporary = Path(temporary_name)
        source = temporary / "source"
        archive = temporary / "archive"
        restored = temporary / "restored"
        source_file = source / "project" / "session.jsonl"
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(b"alpha\n")
        os.chmod(source_file, 0o600)

        backup_once(source, archive)
        with source_file.open("ab") as handle:
            handle.write(b"beta\n")
            handle.flush()
        os.utime(source_file)
        backup_once(source, archive)
        restore_archive(archive, restored)
        if tree_hash(source) != tree_hash(restored):
            raise ArchiveError("restore did not reproduce the source tree")
        serial = backup_once(source, archive)
        if serial != 2:
            raise ArchiveError("unchanged archive backup created a new manifest")

        manifests = manifest_paths(archive)
        if len(manifests) < 2:
            raise ArchiveError("self-test did not create multiple manifests")
        corrupt = manifests[-1]
        original_corrupt = corrupt.read_bytes()
        corrupt.write_text("{ broken", encoding="utf-8")
        fallback = temporary / "fallback"
        restore_archive(archive, fallback)
        if b"alpha\n" not in (fallback / "project" / "session.jsonl").read_bytes():
            raise ArchiveError("valid manifest fallback did not work")
        corrupt.write_bytes(original_corrupt)

        latest = manifest_paths(archive)[-1]
        latest_entry = json.loads(latest.read_text(encoding="utf-8"))["files"][0]
        latest_chunk_path = archive / object_path(latest_entry["chunks"][-1]["object"])
        original_chunk = latest_chunk_path.read_bytes()
        latest_chunk_path.write_bytes(original_chunk + b"x")
        fallback_chunk = temporary / "fallback-chunk"
        restore_archive(archive, fallback_chunk)
        if b"alpha\n" not in (fallback_chunk / "project" / "session.jsonl").read_bytes():
            raise ArchiveError("corrupt chunk fallback did not work")
        latest_chunk_path.write_bytes(original_chunk)

        with source_file.open("r+b") as handle:
            handle.truncate(3)
            handle.flush()
        os.utime(source_file)
        backup_once(source, archive)
        rebuilt = temporary / "rebuilt"
        restore_archive(archive, rebuilt)
        if (rebuilt / "project" / "session.jsonl").read_bytes() != b"alp":
            raise ArchiveError("truncated file was not rebuilt")


def add_path_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--source", type=Path, default=Path("/tmp/dsh-sessions"))
    command.add_argument("--archive", type=Path, default=Path("/data/dsh/session-archive-v1"))


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="commit one incremental archive manifest")
    add_path_arguments(backup)

    restore = subparsers.add_parser("restore", help="restore the newest valid archive manifest")
    restore.add_argument("--archive", type=Path, default=Path("/data/dsh/session-archive-v1"))
    restore.add_argument("--destination", type=Path, default=Path("/tmp/dsh-sessions"))

    watch_parser = subparsers.add_parser("watch", help="archive the session store until SIGTERM")
    add_path_arguments(watch_parser)
    watch_parser.add_argument("--interval", type=positive_int, default=30)
    watch_parser.add_argument("--max-failures", type=nonnegative_int, default=0)

    subparsers.add_parser("self-test", help="run built-in archive consistency checks")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "backup":
            backup_once(args.source.absolute(), args.archive.absolute())
        elif args.command == "restore":
            restore_archive(args.archive.absolute(), args.destination.absolute())
        elif args.command == "watch":
            watch(args.source.absolute(), args.archive.absolute(), args.interval, args.max_failures)
        elif args.command == "self-test":
            self_test()
        else:
            raise ArchiveError(f"unsupported command: {args.command}")
    except Exception as error:
        print(f"[dsh-hfs] session archive error: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
