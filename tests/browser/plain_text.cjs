const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = process.argv[2];
    await page.goto(`${base}/analyses/lichess/q7ZvsdUF?trainer=1`);
    await page.getByRole('textbox', { name: 'Discuss this Position' }).fill('Explain this position.');
    await page.getByRole('button', { name: 'Send to Trainer', exact: true }).click();
    const response = page.getByRole('region', { name: 'Trainer response', exact: true });
    await response.getByText('Responding…', { exact: true }).waitFor();
    const partial = 'Plano\n\nNf3 desenvolve.\n';
    await page.waitForFunction(expected => {
      const region = document.querySelector('[aria-label="Trainer response"]');
      return region && [...region.querySelectorAll('p')].some(p => p.textContent === expected);
    }, partial);
    assert.equal(await response.count(), 1);
    assert.doesNotMatch(await response.innerText(), /[#*]/);
    assert.equal((await page.request.post(`${base}/_test/release`)).status(), 200);
    const final = `${partial}Ainda calculando.`;
    await page.waitForFunction(expected => {
      const region = document.querySelector('[aria-label="Trainer response"]');
      return region && !region.querySelector('[role="status"]') &&
        [...region.querySelectorAll('p')].some(p => p.textContent === expected);
    }, final);
    assert.equal(await response.count(), 1);
    assert.equal(await response.locator('p').textContent(), final);
    await page.reload();
    await response.waitFor();
    assert.equal(await response.count(), 1);
    assert.equal(await response.locator('p').textContent(), final);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
