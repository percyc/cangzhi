#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CANGZHI_ROOT="$(cd "$PLUGIN_DIR/../.." && pwd)"
DSH_SOURCE="${DSH_SOURCE:-/home/percy/software/deepseek-harness}"
DSH_PROFILE="${DSH_PROFILE:-web}"

if [[ ! -f "$DSH_SOURCE/package.json" || ! -f "$DSH_SOURCE/apps/cli/lib/bin.js" ]]; then
  echo "DSH_SOURCE 无效：$DSH_SOURCE" >&2
  exit 1
fi

echo "[1/4] 构建 DSH 首页扩展槽位"
(cd "$DSH_SOURCE" && pnpm --filter @deepseek-ai/dsh-client-ui-conversation run bundle)

echo "[2/4] 检查 DSH conversation UI 类型"
(cd "$DSH_SOURCE" && pnpm exec tsc -b packages/client/ui-conversation/tsconfig.json --pretty false)

echo "[3/4] 构建藏知 DSH 插件"
(
  cd "$PLUGIN_DIR"
  DSH_SOURCE="$DSH_SOURCE" "$DSH_SOURCE/node_modules/.bin/tsdown" --config tsdown.config.ts
  node scripts/rewrite-client-id.mjs
  node --check lib/index.js
  node --check lib/client.js
)

echo "[4/4] 安装到 DSH Profile：$DSH_PROFILE"
(
  cd "$DSH_SOURCE"
  node apps/cli/lib/bin.js plugin --profile "$DSH_PROFILE" remove dsh-cangzhi >/dev/null 2>&1 || true
  node apps/cli/lib/bin.js plugin --profile "$DSH_PROFILE" add "file:$PLUGIN_DIR"
)

echo "安装完成。藏知项目：$CANGZHI_ROOT"
echo "下一步：$PLUGIN_DIR/scripts/start-dsh.sh"
