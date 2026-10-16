const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH});
  const base = process.argv[2];
  let release;
  const held = new Promise(resolve => { release = resolve; });
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 800}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const url = `${base}/analyses/lichess/q7ZvsdUF/discussions`;
    let intercepted;
    const requested = new Promise(resolve => { intercepted = resolve; });
    await page.route(`${url}/0/fragment*`, async route => {
      const response = await route.fetch();
      intercepted();
      await held;
      await route.fulfill({response});
    });
    await page.goto(`${base}/analyses/lichess/q7ZvsdUF?trainer=1`);
    await requested;
    await page.getByRole('button', {name: 'Game', exact: true}).click();
    await page.getByRole('button', {name: 'Next →', exact: true}).click();
    const input = page.getByRole('textbox', {name: 'Discuss this Position'});
    await input.fill('Destination while origin fragment is pending');
    await page.getByRole('button', {name: 'Send to Trainer', exact: true}).click();
    await page.getByText('Destination explanation.', {exact: true}).waitFor({timeout: 3000});
    const status = page.locator('[aria-label="Game navigation"] output');
    const destinationStatus = await status.innerText();
    await input.fill('Keep destination draft');
    release();
    await page.waitForTimeout(150);
    assert.equal(await status.innerText(), destinationStatus);
    const history = page.getByRole('region', {name: 'Discussion history'});
    assert.equal(await history.getByText('Earlier origin explanation.', {exact: true}).count(), 0);
    assert.equal(await history.getByText('Destination explanation.', {exact: true}).count(), 1);
    assert.equal(await input.inputValue(), 'Keep destination draft');
    assert.deepEqual(errors, []);
  } finally {
    release();
    await fetch(`${base}/test/release`, {method: 'POST'});
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
