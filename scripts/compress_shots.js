// 压缩截图：PNG → JPEG(质量 86)，长边限 1800px
// 目的：GitHub 仓库不因截图膨胀，同时保证界面文字仍清晰可读
const sharp = require('sharp');
const fs = require('fs');
const path = require('path');

const SRC = 'shots';
const OUT = 'shots_jpg';

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const files = fs.readdirSync(SRC).filter(f => f.endsWith('.png')).sort();
  let before = 0, after = 0;

  for (const f of files) {
    const src = path.join(SRC, f);
    const dst = path.join(OUT, f.replace(/\.png$/, '.jpg'));
    const b = fs.statSync(src).size;
    before += b;

    let img = sharp(src).flatten({ background: '#0b0d10' });
    const meta = await sharp(src).metadata();
    if (Math.max(meta.width, meta.height) > 1800) {
      img = img.resize({ width: 1800, withoutEnlargement: true });
    }
    await img.jpeg({ quality: 86, progressive: true, mozjpeg: true }).toFile(dst);

    const a = fs.statSync(dst).size;
    after += a;
    console.log(`${f.padEnd(28)} ${String(Math.round(b / 1024)).padStart(5)}K → ${String(Math.round(a / 1024)).padStart(4)}K`);
  }

  console.log('-'.repeat(46));
  console.log(`合计${' '.repeat(22)}${(before / 1048576).toFixed(1)}M → ${(after / 1048576).toFixed(1)}M`);
})();
