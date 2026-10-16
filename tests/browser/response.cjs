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
      const move = line.getByRole('button', { name, exact: true });
      await move.click();
      await page.getByText(`Discutindo a posição após ${name}`, { exact: true }).waitFor();
      assert.equal(await move.getAttribute('aria-pressed'), 'true');
      await page.getByRole('status').filter({ hasText: `Discussion · ${index + 1} of ${index + 1}` }).waitFor();
      const current = await (await page.request.get(endpoint)).json();
      assert.equal(current.activeFen, initial.activeFen);
      assert.deepEqual(current.nodes, initial.nodes);
    }
    // An earlier response move becomes the explicit next-send Position, not a
    // selection mutation just to read the saved Discussion.
    await line.getByRole('button', { name: '1. e4', exact: true }).click();
    await page.getByRole('textbox', { name: 'Discuss this Position' }).fill('Explain this earlier move');
    const sent = page.waitForResponse(r => r.url() === endpoint.slice(0, -2) && r.request().method() === 'POST');
    await page.getByRole('button', { name: 'Send to Trainer', exact: true }).click();
    assert.equal((await sent).status(), 202);
    assert.equal((await (await page.request.get(endpoint)).json()).activeNodeId, initial.nodes[0].id);
    await page.reload();
    await response.first().waitFor();
    assert.equal(await response.count(), 2);
    assert.equal(await response.first().getByRole('group', { name: 'Demonstrated line', exact: true }).getByRole('button').count(), 3);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
