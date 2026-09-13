// Optional local visual check. Requires Playwright; never accesses robot endpoints.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

(async () => {
  const url = process.argv[2];
  assert.equal(new URL(url).hostname, '127.0.0.1');
  const output = process.argv[3];
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.RECEPTION_TEST_BROWSER || 'chrome' });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    for (const width of [920, 360, 320]) {
      await page.setViewportSize({ width, height: 940 });
      await page.goto(url);
      await page.locator('#phase').filter({ hasText: /^Ready$/ }).waitFor();
      assert.equal(await page.locator('#record_video').innerText(), 'Disabled');
      assert.equal(await page.locator('#audio').innerText(), 'Receiving');
      assert.equal(await page.locator('button').count(), 0);
      assert(await page.locator('.brand img').evaluate(el => el.complete && el.naturalWidth > 0));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `ready-${width}.png`), fullPage: true });
    }
    for (const [state, phase] of [['starting', 'Starting'], ['stopping', 'Stopping'], ['stopped', 'Stopped'],
                                ['faulted', 'Needs attention'], ['stale', 'Status stale'], ['disconnected', 'Status unavailable']]) {
      await page.goto(`${url}?state=${state}`);
      await page.locator('#phase').filter({ hasText: new RegExp(`^${phase}$`) }).waitFor();
      assert.notEqual(await page.locator('#audio').innerText(), 'Receiving');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await page.screenshot({ path: path.join(output, 'disconnected-320.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('Passed: desktop/mobile layout, loaded assets, lifecycle states, stale/disconnected health, no page errors.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
