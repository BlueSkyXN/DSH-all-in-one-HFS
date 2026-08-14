#!/usr/bin/env bash
set -euo pipefail

readonly bucket_id="${DSH_HFS_BUCKET_ID:-BlueSkyXN/dsh-all-in-one-hfs-data}"
readonly space_id="${DSH_HFS_SPACE_ID:-BlueSkyXN/DSH-all-in-one-HFS}"
readonly data_prefix="${DSH_HFS_DATA_PREFIX:-dsh}"
readonly seed_path="${data_prefix}/.hfs-bucket-seed"
readonly seed_content='{}'

if [[ "${1:-}" == "--apply" ]]; then
  printf '%s\n' "${seed_content}" \
    | hf buckets cp - "hf://buckets/${bucket_id}/${seed_path}"
elif [[ "${1:-}" != "--check" ]]; then
  printf 'Usage: %s --check | --apply\n' "$0" >&2
  exit 2
fi

seed_readback="$(hf buckets cp "hf://buckets/${bucket_id}/${seed_path}" -)"
volumes_json="$(hf spaces volumes list "${space_id}" --json)"

BUCKET_ID="${bucket_id}" SEED_PATH="${seed_path}" \
  SEED_CONTENT="${seed_readback}" \
  VOLUMES_JSON="${volumes_json}" python3 -c '
import json
import os

seed = os.environ["SEED_PATH"]
if os.environ["SEED_CONTENT"] != "{}":
    raise SystemExit(f"unexpected bucket prefix seed content: {seed}; run with --apply")

volumes = json.loads(os.environ["VOLUMES_JSON"])
expected_source = os.environ["BUCKET_ID"]
if not any(
    item.get("type") == "bucket"
    and item.get("source") == expected_source
    and (item.get("mount_path") or item.get("mountPath")) == "/data"
    and not (item.get("read_only") or item.get("readOnly"))
    for item in volumes
):
    raise SystemExit(f"{expected_source} is not mounted read-write at /data")

print(f"bucket prefix and /data mount: PASS ({seed})")
'
