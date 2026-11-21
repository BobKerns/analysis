// cspell:ignore swiftshader
// Renders the 3D stream scene (stream.html) to stream-scene.png and
// stream-strip.png. WebGL runs in software (SwiftShader), so this needs no GPU;
// three.js loads from jsdelivr, so it needs the network.
const {chromium} = require('playwright');
const http = require('http'), fs = require('fs'), path = require('path');
// ES modules and the import map need http://, not file://.
const srv = http.createServer((q, r) => {
  const f = path.join(__dirname, path.normalize(decodeURIComponent(q.url.split('?')[0])));
  fs.readFile(f, (e, d) => {
    if (e) { r.writeHead(404); return r.end(); }
    r.writeHead(200, {'Content-Type': f.endsWith('.js') ? 'text/javascript' : f.endsWith('.html') ? 'text/html' : 'application/octet-stream'});
    r.end(d);
  });
}).listen(8765);
const views = [['hero', 'stream-scene', 1600, 800], ['banner', 'stream-strip', 1600, 232]];
(async () => {
  const b = await chromium.launch({args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']});
  for (const [view, out, w, h] of views) {
    const p = await b.newPage({viewport: {width: w, height: h}});
    p.on('pageerror', e => console.log('ERR', e.message));
    await p.goto(`http://localhost:8765/stream.html?view=${view}&w=${w}&h=${h}`);
    await p.waitForFunction('window.DONE', null, {timeout: 180000});
    await p.screenshot({path: path.join(__dirname, out + '.png')});
    await p.close();
  }
  await b.close(); srv.close();
})();
