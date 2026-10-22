/* eslint-disable @typescript-eslint/no-require-imports -- Offline asset export. */
// Format/size export only: keep the imagegen source and its alpha unchanged.
const path = require('node:path');
const fs = require('node:fs/promises');
const sharp = require('sharp');

async function main() {
  const source = path.resolve(__dirname, '../../../docs/brand/cangzhi-icon-v1-source.png');
  const output = path.resolve(__dirname, '../public/brand');
  const metadata = await sharp(source).metadata();
  if (metadata.width !== metadata.height || !metadata.hasAlpha) {
    throw new Error('Brand source must be square and preserve transparency.');
  }
  await fs.mkdir(output, { recursive: true });
  for (const size of [32, 48, 96, 180, 512]) {
    const target = path.join(output, `cangzhi-icon-v1-${size}.png`);
    await sharp(source).resize(size, size, { kernel: 'lanczos3' })
      .png({ compressionLevel: 9, palette: false }).toFile(target);
    console.log(`${path.relative(process.cwd(), target)} (${size} × ${size})`);
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
