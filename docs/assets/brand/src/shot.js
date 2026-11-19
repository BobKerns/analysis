// Renders banner.html, hero.html and social-card.html into ../ (docs/assets/brand).
const {chromium} = require('playwright');
const path = require('path');
const pages = [['banner', 1280, 186], ['hero', 1280, 640], ['social-card', 1280, 640]];
(async () => {
  const b = await chromium.launch();
  for (const [n, w, h] of pages) {
    const p = await b.newPage({viewport: {width: w, height: h}, deviceScaleFactor: 1});
    await p.goto('file://' + path.join(__dirname, n + '.html'));
    await p.evaluate(() => document.fonts.ready);
    await p.waitForTimeout(500);
    await p.screenshot({path: path.join(__dirname, '..', n + '.jpg'), type: 'jpeg', quality: 88});
    await p.close();
  }
  await b.close();
})();
