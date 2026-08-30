import { readFile, writeFile } from 'node:fs/promises'

const FROM = '@deepseek-ai/dsh-client-ui-cordis'
const TO = 'dsh-cangzhi'

for (const path of ['lib/client.js', 'lib/client.js.map']) {
  let source
  try {
    source = await readFile(path, 'utf8')
  } catch (error) {
    if (path.endsWith('.map') && error?.code === 'ENOENT') continue
    throw error
  }
  if (path === 'lib/client.js' && !source.includes(FROM) && !source.includes(TO)) {
    throw new Error(`${path} does not contain the expected temporary client id`)
  }
  if (source.includes(FROM)) await writeFile(path, source.replaceAll(FROM, TO))
}
