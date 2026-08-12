// 실제 UI 를 사람이 쓰듯 몰아 본다. 배포된 페이지를 열고, 채널에서 번들을 고르고,
// ROM 과 키를 파일 입력에 넣고, 버튼을 눌러 결과 표가 뜨는지 본다.
//
//   node web/run_ui.mjs https://krpatch.duckdns.org/apply/ <rom> <keys>
import puppeteer from '/root/tmp/gjc-wt-escesc/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js';

const [url, romPath, keysPath] = process.argv.slice(2);
const browser = await puppeteer.launch({
  executablePath: '/usr/bin/chromium',
  headless: true,
  protocolTimeout: 0,
  userDataDir: process.env.CHROME_PROFILE || '/mnt/ssd256/hpk-web/chrome-ui',
  args: ['--no-sandbox', '--disable-dev-shm-usage',
         '--ignore-certificate-errors'],
});
const page = await browser.newPage();
const logs = [];
page.on('console', (m) => { logs.push(m.text()); console.error('[c]', m.text()); });
page.on('pageerror', (e) => logs.push('pageerror: ' + e.message));
await page.goto(url, {waitUntil: 'networkidle2', timeout: 120000});

const report = {url};
const step = (s) => console.error('[step]', s, Math.round(process.uptime()) + 's');
step('loaded');
report.isolated = await page.evaluate(() => self.crossOriginIsolated);
report.problems = await page.$$eval('#requirements li.bad',
                                    (els) => els.map((e) => e.textContent));
step('isolated=' + report.isolated + ' problems=' + report.problems.length);
await page.waitForSelector('#bundle-list .choice', {timeout: 60000});
report.bundles = await page.$$eval('#bundle-list .choice strong',
                                   (els) => els.map((e) => e.textContent));

step('bundles ' + report.bundles.join(','));
await (await page.$('#file-rom')).uploadFile(romPath);
step('rom uploaded');
if (keysPath) await (await page.$('#file-keys')).uploadFile(keysPath);
await page.click('#bundle-list .choice input');
step('bundle chosen');
await page.waitForFunction(
    () => !document.getElementById('start').disabled, {timeout: 120000});
report.plan = await page.$eval('#plan', (e) => e.textContent);
step('plan: ' + report.plan);

const t0 = Date.now();
await page.click('#start');
step('started');
const poll = setInterval(async () => {
  try {
    const p = await page.$eval('#phase', (e) => e.textContent);
    const l = await page.$eval('#log', (e) => e.textContent.trim().split('\n').pop());
    console.error('[run]', p, '|', l);
  } catch (e) {}
}, 20000);
await page.waitForSelector('#result h3', {timeout: 3600000, polling: 2000});
clearInterval(poll);
report.seconds = Math.round((Date.now() - t0) / 1000);
report.heading = await page.$eval('#result h3', (e) => e.textContent.trim());
report.table = await page.$$eval('#result tr', (rows) => rows.map(
    (r) => r.cells[0].textContent.trim() + ': ' + r.cells[1].textContent.trim()));
report.saveButton = !!(await page.$('#save'));
report.tail = (await page.$eval('#log', (e) => e.textContent)).trim()
    .split('\n').slice(-6);
console.log(JSON.stringify(report, null, 1));
await browser.close();
