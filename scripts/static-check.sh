#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly root
cd "${root}"

bash -n entrypoint.sh
bash -n scripts/prepare-bucket-prefix.sh

ROOT="${root}" python3 - <<'PY'
import json
import os
import pathlib
import tomllib

root = pathlib.Path(os.environ["ROOT"])

with (root / "hfs-dev.toml").open("rb") as handle:
    manifest = tomllib.load(handle)

expected_manifest = {
    "standard": "3.0",
    "space": "BlueSkyXN/DSH-all-in-one-HFS",
    "sovereignty": "port",
    "lane": "artifact",
    "version_source": "tag",
    "space_visibility": "protected",
    "bucket_visibility": "private",
    "env_file": "local/hfs-targets/production.env",
}
for key, value in expected_manifest.items():
    if manifest.get(key) != value:
        raise SystemExit(f"hfs-dev.toml has unexpected {key}: {manifest.get(key)!r}")

package = json.loads((root / "package.json").read_text(encoding="utf-8"))
lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
expected_version = "0.2.2-rc.5"
if package.get("dependencies", {}).get("@deepseek-ai/dsh") != expected_version:
    raise SystemExit("package.json does not pin the expected dsh version")
if lock.get("packages", {}).get("", {}).get("dependencies", {}).get("@deepseek-ai/dsh") != expected_version:
    raise SystemExit("package-lock.json does not pin the expected dsh version")
dsh_lock = lock.get("packages", {}).get("node_modules/@deepseek-ai/dsh", {})
if dsh_lock.get("version") != expected_version:
    raise SystemExit("package-lock.json resolved an unexpected dsh version")
if dsh_lock.get("integrity") != "sha512-cNbp12tLYLUTXaVli7zwwMjzmmEVCD1PRldg4oKtXM4+lFYckcaliMS2PU/FoThB4iAN/fKOSID0nrtRZU4Gjw==":
    raise SystemExit("package-lock.json resolved an unexpected dsh integrity")
if "@deepseek-ai/dsh-session-persistence-sqlite" in lock.get("packages", {}):
    raise SystemExit("package-lock.json still contains the removed SQLite persistence package")

readme = (root / "README.md").read_text(encoding="utf-8")
for line in ("sdk: docker", "app_port: 7860", "license: gpl-3.0"):
    if line not in readme:
        raise SystemExit(f"README.md is missing Space metadata: {line}")

dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
entrypoint = (root / "entrypoint.sh").read_text(encoding="utf-8")
nginx = (root / "nginx.conf").read_text(encoding="utf-8")
bucket_script = (root / "scripts/prepare-bucket-prefix.sh").read_text(encoding="utf-8")
space_overlay = (root / "space.cordis.yml").read_text(encoding="utf-8")

for snippet in (
    "@deepseek-ai/dsh@${DSH_VERSION}",
    "DSH_HOME=/data/dsh",
    "USER node",
    "EXPOSE 7860",
    '["/usr/bin/tini", "--"]',
):
    if snippet not in dockerfile:
        raise SystemExit(f"Dockerfile is missing runtime contract: {snippet}")

for snippet in (
    'fail "/data must be a mounted persistent Hugging Face bucket"',
    '--trusted-host "${trusted_host}"',
    'htpasswd -imc',
    'nginx -c /etc/nginx/nginx.conf',
    'verify_writable_directory "${workspace}"',
    'verify_session_store',
    '--patch "${space_overlay}"',
    '--no-open',
    'readonly launch_token_file="/tmp/dsh-nginx/launch-token.conf"',
    's#([?&]token=)[A-Za-z0-9_-]+#\\1<redacted>#g',
):
    if snippet not in entrypoint:
        raise SystemExit(f"entrypoint.sh is missing runtime contract: {snippet}")

for snippet in (
    'listen 7860;',
    'listen 127.0.0.1:7861;',
    'log_format dsh_hfs',
    '"$request_method $uri $server_protocol"',
    'include /tmp/dsh-nginx/launch-token.conf;',
    'auth_basic "DeepSeek Harness";',
    'error_page 401 = @dsh_launch;',
    'return 302 "/?token=${dsh_launch_token}";',
    'proxy_pass http://127.0.0.1:3080;',
    'proxy_set_header Host $http_host;',
    'proxy_buffering off;',
):
    if snippet not in nginx:
        raise SystemExit(f"nginx.conf is missing proxy contract: {snippet}")

for snippet in (
    '${data_prefix}/.hfs-bucket-seed',
    '${data_prefix}/home/.hfs-bucket-seed',
    '${data_prefix}/workspace/.hfs-bucket-seed',
):
    if snippet not in bucket_script:
        raise SystemExit(f"prepare-bucket-prefix.sh is missing seed path: {snippet}")

for snippet in (
    "- id: session-persistence-jsonl",
    "root: /tmp/dsh-sessions",
):
    if snippet not in space_overlay:
        raise SystemExit(f"space.cordis.yml is missing persistence contract: {snippet}")

print("static contract: PASS")
PY

if [[ -d .git ]]; then
  git diff --check
fi

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck entrypoint.sh scripts/static-check.sh scripts/prepare-bucket-prefix.sh
fi

if command -v hadolint >/dev/null 2>&1; then
  hadolint Dockerfile
fi

printf 'static checks: PASS\n'
