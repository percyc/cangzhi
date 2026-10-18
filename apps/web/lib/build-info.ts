export type BuildInfo = { version: string; revision: string; built_at: string; dirty: boolean };

// Next freezes these values at build time for both browser and server bundles.
export const WEB_BUILD: BuildInfo = {
  version: process.env.NEXT_PUBLIC_CANGZHI_VERSION || 'unversioned',
  revision: process.env.NEXT_PUBLIC_CANGZHI_REVISION || 'unknown',
  built_at: process.env.NEXT_PUBLIC_CANGZHI_BUILD_TIME || 'unknown',
  dirty: process.env.NEXT_PUBLIC_CANGZHI_BUILD_DIRTY !== 'false',
};

export function buildLabel(build: BuildInfo): string {
  if (build.version === 'unversioned') return '未标记构建';
  return `${build.version} · ${build.revision === 'unknown' ? '提交未知' : build.revision.slice(0, 7)}`;
}

export function compareBuilds(web: BuildInfo, api: BuildInfo): 'unknown' | 'mismatch' | 'match' {
  if (![web.revision, api.revision].every(value => /^[a-f0-9]{40}$/.test(value)) || web.dirty || api.dirty) return 'unknown';
  return web.revision === api.revision && web.version === api.version ? 'match' : 'mismatch';
}
