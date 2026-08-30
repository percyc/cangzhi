#!/usr/bin/env bash
set -euo pipefail

DSH_SOURCE="${DSH_SOURCE:-/home/percy/software/deepseek-harness}"
DSH_PROFILE="${DSH_PROFILE:-web}"
DSH_HOST="${DSH_HOST:-127.0.0.1}"
DSH_PORT="${DSH_PORT:-3080}"
CANGZHI_API_URL="${CANGZHI_API_URL:-http://127.0.0.1:8000}"
CANGZHI_API_BASE="${CANGZHI_API_URL%/}"

if [[ ! -f "$DSH_SOURCE/apps/cli/lib/bin.js" ]]; then
  echo "DSH_SOURCE 无效：$DSH_SOURCE" >&2
  exit 1
fi

if command -v curl >/dev/null 2>&1; then
  if ! curl --silent --show-error --fail --max-time 3 "$CANGZHI_API_BASE/api/readiness" >/dev/null; then
    echo "藏知 API 未就绪：$CANGZHI_API_BASE/api/readiness" >&2
    echo "请先启动 postgres、api 和 worker。" >&2
    exit 1
  fi
fi

if command -v ss >/dev/null 2>&1; then
  if ss -ltn | grep -Eq ":${DSH_PORT}[[:space:]]"; then
    echo "DSH 端口 $DSH_PORT 已被占用，请先停止旧 DSH。" >&2
    exit 1
  fi
  if ss -ltn | grep -Eq ':3081[[:space:]]'; then
    echo "内部 MCP 端口 3081 已被占用，请先停止旧 DSH。" >&2
    exit 1
  fi
fi

echo "启动藏知 DSH：$DSH_HOST:$DSH_PORT"
echo "藏知 API：$CANGZHI_API_BASE"
cd "$DSH_SOURCE"
exec env -u CANGZHI_MCP_URL \
  CANGZHI_API_URL="$CANGZHI_API_BASE" \
  node apps/cli/lib/bin.js --profile "$DSH_PROFILE" \
  --host "$DSH_HOST" --port "$DSH_PORT" --no-open
