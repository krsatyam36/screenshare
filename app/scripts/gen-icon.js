// Rasterize the logo SVG → build/icon.png (electron-builder needs a PNG).
// Runs on postinstall and before `dist`. No-ops gracefully if sharp/SVG absent.
const fs = require('fs');
const path = require('path');

const svg = path.join(__dirname, '..', '..', 'assets', 'screenshare.svg');
const out = path.join(__dirname, '..', 'build', 'icon.png');

(async () => {
  try {
    if (fs.existsSync(out) && !process.env.FORCE_ICON) return;
    if (!fs.existsSync(svg)) { console.warn('gen-icon: logo svg not found'); return; }
    const sharp = require('sharp');
    fs.mkdirSync(path.dirname(out), { recursive: true });
    await sharp(svg, { density: 384 }).resize(512, 512).png().toFile(out);
    console.log('gen-icon: wrote', out);
  } catch (e) {
    console.warn('gen-icon: skipped (', e.message, ')');
  }
})();
