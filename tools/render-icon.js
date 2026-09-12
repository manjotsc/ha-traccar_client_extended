/*
 * Rasterise the icon for the home-assistant/brands repository.
 *
 * icon.svg is the source of truth; these PNGs are generated from it. brands
 * requires PNG, which is the only reason a build step exists at all.
 *
 *   npm install sharp        (once, in this directory)
 *   node tools/render-icon.js
 */
const path = require('path');
const sharp = require(path.join(__dirname, 'node_modules', 'sharp'));

const DIR = path.join(__dirname, '..', 'brands');
const SRC = path.join(DIR, 'icon.svg');

// density high enough that the vector is rasterised above the target size and
// downsampled, rather than drawn at exactly the output resolution.
const jobs = [
  ['icon.png', 256],
  ['icon@2x.png', 512],
];

(async () => {
  for (const [name, size] of jobs) {
    const out = path.join(DIR, name);
    const info = await sharp(SRC, { density: 1200 })
      .resize(size, size, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 } })
      .png({ compressionLevel: 9, palette: false })
      .toFile(out);
    console.log(`  brands/${name}  ${info.width}x${info.height}  ${info.size} B`);
  }
})().catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
