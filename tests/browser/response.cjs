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
    await page.goto(`${process.argv[2]}/analyses/lichess/q7ZvsdUF?trainer=1`);
    const response = page.getByRole('region', { name: 'Trainer response', exact: true });
    await response.waitFor();
    assert.equal(await response.count(), 1);
    assert.match(await response.innerText(), /Consider Nf3\./);
    const line = response.getByRole('group', { name: 'Demonstrated line', exact: true });
    assert.equal(await line.count(), 1);
    assert.deepEqual(await line.getByRole('button').allTextContents(), ['1. e4', '1... e5', '2. Nf3']);
    const endpoint = `${process.argv[2]}/analyses/lichess/q7ZvsdUF/discussions/0`;
    const initial = await (await page.request.get(endpoint)).json();
    for (const [index, name] of ['1. e4', '1... e5', '2. Nf3'].entries()) {
      const saved = page.waitForResponse(r => r.url() === `${endpoint}/selection` && r.request().method() === 'POST');
      await line.getByRole('button', { name, exact: true }).click();
      assert.equal((await saved).status(), 200);
      await page.waitForFunction(({ node }) => {
        const buttons = [...document.querySelectorAll('[aria-label="Demonstrated line"] button')];
        return buttons.some(button => button.dataset.nodeId === String(node) && button.getAttribute('aria-pressed') === 'true');
      }, { node: initial.nodes[index].id });
      const current = await (await page.request.get(endpoint)).json();
      assert.equal(current.activeFen, initial.nodes[index].fen);
      assert.deepEqual(current.nodes, initial.nodes);
    }
    await page.reload();
    await response.waitFor();
    assert.equal(await response.count(), 1);
    assert.equal(await line.getByRole('button').count(), 3);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
