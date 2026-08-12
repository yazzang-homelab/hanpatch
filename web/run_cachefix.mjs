// 실패를 먼저 재현하고 나서 고쳐졌음을 보인다.
//
// 사고: /apply 가 잘못된 MIME(application/octet-stream)으로 pyodide.mjs 를 주던
// 14분 동안 페이지를 연 브라우저는 그 응답을 일주일치로 캐시했다. 모듈 import 는
// MIME 을 검사하므로 서버를 고친 뒤에도 그 브라우저는 요청조차 하지 않고 실패한다.
//
// 이 스크립트는 로컬 서버로 그 나쁜 응답을 실제로 캐시에 심고(1단계), 옛 경로에서
// import 가 깨지는 것을 확인하고(2단계), 배포된 새 버전 경로에서는 같은 캐시를 가진
// 브라우저도 성공하는지 본다(3단계).
import http from 'node:http';
import fs from 'node:fs';
import puppeteer from '/root/tmp/gjc-wt-escesc/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js';

const FILE = '/mnt/ssd256/krpatch-web/vendor/pyodide-0.26.4/pyodide.mjs';
const body = fs.readFileSync(FILE);
const bad = http.createServer((req, res) => {
  if (req.url === '/apply/vendor/pyodide/pyodide.mjs') {
    res.writeHead(200, {          // 사고 당시와 같은 응답
      'Content-Type': 'application/octet-stream',
      'Cache-Control': 'public, max-age=604800',
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
      'Cross-Origin-Resource-Policy': 'same-origin',
      'Content-Length': body.length,
    });
    res.end(body);
    return;
  }
  if (req.url === '/apply/' || req.url === '/apply/index.html') {
    const html = '<!doctype html><meta charset=utf-8><title>cache probe</title>';
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
      'Content-Length': Buffer.byteLength(html),
    });
    res.end(html);
    return;
  }
  res.writeHead(404).end();
});
await new Promise((r) => bad.listen(8231, '127.0.0.1', r));

const browser = await puppeteer.launch({
  executablePath: '/usr/bin/chromium',
  headless: true,
  protocolTimeout: 0,
  userDataDir: '/mnt/ssd256/hpk-web/chrome-cache-probe',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const out = {};
const page = await browser.newPage();

// 1단계: 나쁜 응답을 캐시에 심는다
await page.goto('http://127.0.0.1:8231/apply/', {waitUntil: 'load'});
out.poisoned = await page.evaluate(async () => {
  const r = await fetch('./vendor/pyodide/pyodide.mjs');
  return {status: r.status, type: r.headers.get('content-type'),
          cache: r.headers.get('cache-control')};
});

// 2단계: 같은 URL 로 import — 서버를 껐으니 캐시에서 온다
bad.close();
out.oldPathImport = await page.evaluate(async () => {
  try {
    await import('./vendor/pyodide/pyodide.mjs');
    return 'imported (기대와 다름)';
  } catch (e) {
    return String(e.message).slice(0, 120);
  }
});

// 3단계: 배포된 버전 경로 — 오염된 캐시를 그대로 들고 간다
const live = await browser.newPage();
live.on('pageerror', (e) => (out.livePageError = e.message));
await live.goto('https://krpatch.duckdns.org/apply/', {waitUntil: 'load'});
out.newPathImport = await live.evaluate(async () => {
  try {
    const m = await import('./vendor/pyodide-0.26.4/pyodide.mjs');
    return typeof m.loadPyodide === 'function' ? 'loadPyodide 있음' : '모듈 이상';
  } catch (e) {
    return 'FAILED ' + String(e.message).slice(0, 120);
  }
});
out.workerBoots = await live.evaluate(() => new Promise((res) => {
  const w = new Worker('worker.js', {type: 'module'});
  const t = setTimeout(() => res('timeout'), 120000);
  w.addEventListener('message', (e) => {
    if (e.data.type === 'ready') { clearTimeout(t); res('ready'); }
    if (e.data.type === 'failed') { clearTimeout(t); res('failed: ' + e.data.error); }
  });
  w.addEventListener('error', (e) => { clearTimeout(t); res('error: ' + e.message); });
  w.postMessage({cmd: 'boot'});
}));
console.log(JSON.stringify(out, null, 1));
await browser.close();
