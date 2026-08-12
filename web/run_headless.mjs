// 헤드리스 크로미움으로 web/apply 의 페이지를 열고 결과를 stdout 으로 받는다.
//   node web/run_headless.mjs http://127.0.0.1:8123/selftest/capabilities.html
// 페이지는 끝나면 document.title 을 'done' 으로 바꾸고 #out 에 JSON 을 남긴다.
import puppeteer from '/root/tmp/gjc-wt-escesc/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js';

const url = process.argv[2];
const timeout = Number(process.argv[3] || 300000);
const browser = await puppeteer.launch({
  executablePath: '/usr/bin/chromium',
  headless: true,
  // CDP 기본 타임아웃이 180초다. 2GB 패치는 그보다 오래 걸린다.
  protocolTimeout: 0,
  // 프로필을 SSD 에 둔다. OPFS 할당량은 남은 디스크에 비례하고, DQ7 한 번에
  // 스크래치 4.3GB + 결과 2GB 가 필요하다.
  userDataDir: process.env.CHROME_PROFILE || '/mnt/ssd256/hpk-web/chrome-profile',
  args: ['--no-sandbox', '--disable-dev-shm-usage',
         '--enable-features=SharedArrayBuffer'],
});
const page = await browser.newPage();
page.on('console', (m) => console.error('[console]', m.type(), m.text()));
page.on('pageerror', (e) => console.error('[pageerror]', e.message));
await page.goto(url, {waitUntil: 'domcontentloaded'});
try {
  await page.waitForFunction('document.title === "done"', {timeout, polling: 500});
} catch (e) {
  console.error('[timeout waiting for done]');
}
const out = await page.evaluate(() => {
  const el = document.getElementById('out');
  return el ? el.textContent : document.body.textContent;
});
console.log(out);
await browser.close();
