#!/usr/bin/env bash
# Resolve metadata on the build machine only. Offline startup must not run this.
set -euo pipefail
build_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CANGZHI_REVISION="$(git -C "$build_root" rev-parse --verify HEAD)"
export CANGZHI_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export CANGZHI_BUILD_DIRTY=false
if [[ -n "$(git -C "$build_root" status --porcelain --untracked-files=normal)" ]]; then
  export CANGZHI_BUILD_DIRTY=true
fi
build_tag="$(git -C "$build_root" describe --tags --exact-match HEAD 2>/dev/null || true)"
if [[ -z "${CANGZHI_VERSION:-}" ]]; then
  if [[ -n "$build_tag" ]]; then
    export CANGZHI_VERSION="$build_tag"
  else
    export CANGZHI_VERSION="$(tr -d '[:space:]' < "$build_root/VERSION")+${CANGZHI_REVISION:0:7}"
  fi
fi
if [[ "$CANGZHI_BUILD_DIRTY" == true ]]; then
  export CANGZHI_VERSION="${CANGZHI_VERSION}.dirty"
fi
exec "$@"
