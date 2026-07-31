#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "用法：scripts/rehearse-restore.sh <备份目录>" >&2
  exit 2
fi

workspace_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="$(cd "$1" && pwd)"
database_user="${POSTGRES_USER:-cangzhi}"
rehearsal_database="cangzhi_restore_check_$$"

if [[ ! "${rehearsal_database}" =~ ^cangzhi_restore_check_[0-9]+$ ]]; then
  echo "内部错误：恢复演练数据库名称不安全" >&2
  exit 1
fi

"${workspace_dir}/scripts/verify-backup.sh" "${backup_dir}"

cleanup() {
  docker compose \
    --project-directory "${workspace_dir}" \
    exec -T postgres \
    dropdb \
    --username="${database_user}" \
    --if-exists \
    "${rehearsal_database}" \
    > /dev/null
}
trap cleanup EXIT

cleanup
docker compose \
  --project-directory "${workspace_dir}" \
  exec -T postgres \
  createdb \
  --username="${database_user}" \
  "${rehearsal_database}"

docker compose \
  --project-directory "${workspace_dir}" \
  exec -T postgres \
  pg_restore \
  --username="${database_user}" \
  --dbname="${rehearsal_database}" \
  --no-owner \
  --no-acl \
  < "${backup_dir}/database.dump"

restored_revision="$(
  docker compose \
    --project-directory "${workspace_dir}" \
    exec -T postgres \
    psql \
    --username="${database_user}" \
    --dbname="${rehearsal_database}" \
    --tuples-only \
    --no-align \
    --command="SELECT version_num FROM alembic_version" \
    | tr -d '[:space:]'
)"
restored_documents="$(
  docker compose \
    --project-directory "${workspace_dir}" \
    exec -T postgres \
    psql \
    --username="${database_user}" \
    --dbname="${rehearsal_database}" \
    --tuples-only \
    --no-align \
    --command="SELECT COUNT(*) FROM documents" \
    | tr -d '[:space:]'
)"

echo "恢复演练通过：schema=${restored_revision} documents=${restored_documents}"
echo "演练数据库 ${rehearsal_database} 将立即删除，正式数据库未被修改。"
