#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "用法：scripts/verify-backup.sh <备份目录>" >&2
  exit 2
fi

workspace_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="$(cd "$1" && pwd)"

for required_file in database.dump storage.tar.gz metadata.json SHA256SUMS; do
  if [[ ! -f "${backup_dir}/${required_file}" ]]; then
    echo "备份不完整：缺少 ${required_file}" >&2
    exit 1
  fi
done

(
  cd "${backup_dir}"
  sha256sum --check SHA256SUMS
)

docker compose \
  --project-directory "${workspace_dir}" \
  exec -T postgres \
  pg_restore \
  --list \
  < "${backup_dir}/database.dump" \
  > /dev/null

tar --list --gzip --file="${backup_dir}/storage.tar.gz" > /dev/null

echo "备份校验通过：${backup_dir}"
