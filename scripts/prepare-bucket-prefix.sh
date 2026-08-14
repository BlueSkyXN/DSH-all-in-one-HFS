#!/usr/bin/env bash
set -euo pipefail

readonly bucket_id="${DSH_HFS_BUCKET_ID:-BlueSkyXN/dsh-all-in-one-hfs-data}"
readonly space_id="${DSH_HFS_SPACE_ID:-BlueSkyXN/DSH-all-in-one-HFS}"
readonly data_prefix="${DSH_HFS_DATA_PREFIX:-dsh}"
readonly seed_content='{}'
readonly -a seed_paths=(
  "${data_prefix}/.hfs-bucket-seed"
  "${data_prefix}/home/.hfs-bucket-seed"
  "${data_prefix}/workspace/.hfs-bucket-seed"
)

if [[ "${1:-}" == "--apply" ]]; then
  for seed_path in "${seed_paths[@]}"; do
    printf '%s\n' "${seed_content}" \
      | hf buckets cp - "hf://buckets/${bucket_id}/${seed_path}"
  done
elif [[ "${1:-}" != "--check" ]]; then
  printf 'Usage: %s --check | --apply\n' "$0" >&2
  exit 2
fi

for seed_path in "${seed_paths[@]}"; do
  seed_readback="$(hf buckets cp "hf://buckets/${bucket_id}/${seed_path}" -)"
  if [[ "${seed_readback}" != "${seed_content}" ]]; then
    printf 'Unexpected bucket prefix seed content: %s; run with --apply\n' "${seed_path}" >&2
    exit 1
  fi
done
volumes_json="$(hf spaces volumes list "${space_id}" --json)"

BUCKET_ID="${bucket_id}" VOLUMES_JSON="${volumes_json}" python3 -c '
import json
import os

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

print("bucket prefixes and /data mount: PASS")
'
