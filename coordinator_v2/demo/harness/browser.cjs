const { chromium } = require('/opt/checks/node_modules/playwright');
const { readFile } = require('node:fs/promises');
const { createServer } = require('node:http');
const assert = require('node:assert/strict');

(async function run() {
  const assets = new Map([
    ['/', ['/case/index.html', 'text/html']],
    ['/client.js', ['/case/client.js', 'text/javascript']],
  ]);
  const server = createServer(async (request, response) => {
    try {
      const asset = assets.get(request.url);
      if (!asset) {
        response.writeHead(404).end();
        return;
      }
      response.writeHead(200, { 'Content-Type': asset[1] }).end(await readFile(asset[0]));
    } catch {
      response.writeHead(500).end();
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({ chromiumSandbox: false, timeout: 15000 });
    const context = await browser.newContext({ serviceWorkers: 'block' });
    let denied = 0;
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin === origin) return route.continue();
      denied += 1;
      return route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', () => errors.push('page error'));
    await page.goto(origin, { timeout: 8000 });
    const list = page.getByRole('list', { name: 'Inventory' }).getByRole('listitem');
    const category = page.getByLabel('Category');
    assert.deepEqual(await list.allTextContents(), ['pencil', 'paper', 'cup']);
    await category.selectOption('office');
    assert.deepEqual(await list.allTextContents(), ['pencil', 'paper']);
    await category.selectOption('outdoor');
    assert.equal(await list.count(), 0);
    assert.equal(await page.getByRole('status', { name: '' }).textContent(), 'No items');
    assert.equal(await page.getByRole('status').isVisible(), true);
    await category.selectOption('all');
    assert.equal(await list.count(), 3);
    assert.equal(await page.getByRole('status').isVisible(), false);
    const outsideReachable = await page.evaluate(() =>
      fetch('http://127.0.0.1:1/blocked').then(() => true, () => false));
    assert.equal(outsideReachable, false);
    assert.ok(denied >= 1);
    assert.deepEqual(errors, []);
    console.log('PASS: synthetic catalog, filter, empty state and reset');
  } finally {
    await browser?.close();
    server.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
