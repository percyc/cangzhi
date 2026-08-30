import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'

const dshSource = process.env.DSH_SOURCE
if (dshSource === undefined || dshSource.length === 0) {
  throw new Error('Set DSH_SOURCE to the local deepseek-harness checkout before building')
}

const presetUrl = pathToFileURL(resolve(dshSource, 'packages/client/tsdown.client.ts')).href
const { clientBundle } = await import(presetUrl)

// The current DSH preset discovers dependency policy from its own monorepo
// manifests. Build through a compatible shipped browser package, then the
// deterministic post-step rewrites only the bundle identity to this tree-out
// package. Runtime externals are the same subset used by that package.
export default clientBundle('@deepseek-ai/dsh-client-ui-cordis', ['src/index.ts'])
