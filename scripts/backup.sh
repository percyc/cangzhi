#!/usr/bin/env bash
set -euo pipefail
umask 077

workspace_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timestamp="$(date -u +%Y%m%d-%H%M%S)"
backup_parent="${1:-${workspace_dir}/backups}"
backup_dir="${backup_parent%/}/cangzhi-${timestamp}"
database_name="${POSTGRES_DB:-cangzhi}"
database_user="${POSTGRES_USER:-cangzhi}"

backup_complete=false
cleanup_incomplete_backup() {
  if [[ "${backup_complete}" == "false" && -d "${backup_dir}" ]]; then
    find "${backup_dir}" -depth -delete
  fi
}
trap cleanup_incomplete_backup EXIT

mkdir -p "${backup_dir}"

docker compose \
  --project-directory "${workspace_dir}" \
  exec -T postgres \
  pg_dump \
  --username="${database_user}" \
  --dbname="${database_name}" \
  --format=custom \
  --no-owner \
  --no-acl \
  > "${backup_dir}/database.dump"

docker compose \
  --project-directory "${workspace_dir}" \
  exec -T api \
  tar \
  --directory=/app \
  --create \
  --gzip \
  --file=- \
  storage \
  > "${backup_dir}/storage.tar.gz"

git_revision="$(
  git -C "${workspace_dir}" rev-parse --verify HEAD 2>/dev/null || true
)"
schema_revision="$(
  docker compose \
    --project-directory "${workspace_dir}" \
    exec -T postgres \
    psql \
    --username="${database_user}" \
    --dbname="${database_name}" \
    --tuples-only \
    --no-align \
    --command="SELECT version_num FROM alembic_version" \
    | tr -d '[:space:]'
)"
document_count="$(
  docker compose \
    --project-directory "${workspace_dir}" \
    exec -T postgres \
    psql \
    --username="${database_user}" \
    --dbname="${database_name}" \
    --tuples-only \
    --no-align \
    --command="SELECT COUNT(*) FROM documents" \
    | tr -d '[:space:]'
)"

cat > "${backup_dir}/metadata.json" <<EOF
{
  "format": "cangzhi.backup.v1",
  "created_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "git_revision": "${git_revision}",
  "schema_revision": "${schema_revision}",
  "database_name": "${database_name}",
  "document_count": ${document_count}
}
EOF

(
  cd "${backup_dir}"
  sha256sum database.dump storage.tar.gz metadata.json > SHA256SUMS
)

backup_complete=true
echo "备份已创建：${backup_dir}"
echo "下一步运行：scripts/verify-backup.sh ${backup_dir}"
