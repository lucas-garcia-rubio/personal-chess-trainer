const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH });
  let height = Number(process.argv[6]);
  const returnLatest = process.argv[5] === 'True';
  const page = await browser.newPage({ viewport: { width: 1280, height } });
  const base = process.argv[2];
  const endpoint = `${base}/analyses/lichess/q7ZvsdUF/discussions`;
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(`${base}/analyses/lichess/q7ZvsdUF?trainer=1`);
    const history = page.getByRole('region', { name: 'Discussion history', exact: true });
    const input = page.getByRole('textbox', { name: 'Discuss this Position' });
    await input.fill('Explain further');
    await history.evaluate(el => { el.scrollTop = el.scrollHeight; });
    await page.waitForTimeout(100);
    await page.getByRole('button', { name: 'Send to Trainer' }).click();
    const response = page.getByRole('region', { name: 'Trainer response', exact: true }).last();
    const tail = () => history.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop);
    async function chunk(index) {
      fs.writeFileSync(`${process.argv[3]}/${index}`, 'release');
      await page.waitForFunction(i => document.querySelector('[aria-label="Discussion history"]').textContent.includes(`End chunk ${i}.`), index);
      await page.waitForTimeout(100);
    }
    await chunk(1);
    assert.ok(await tail() < 3, 'new streaming text follows the tail');
    await chunk(2);
    assert.ok(await tail() < 3, 'continued response growth follows the tail');
    await history.hover();
    await page.mouse.wheel(0, -700);
    await page.waitForTimeout(150);
    let reading = await history.evaluate(el => el.scrollTop);
    assert.ok(await tail() > 500);
    const jump = page.getByRole('button', { name: 'Ir para a resposta mais recente', exact: true });
    await jump.waitFor();
    async function tool(name, extra = {}) {
      const saved = await (await page.request.get(`${endpoint}/0`)).json();
      const result = await (await page.request.post(`${base}/trainer/mcp`, {
        headers: { Authorization: `Bearer ${process.argv[4]}` },
        data: { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: {
          attempt_id: saved.attempts.at(-1).id,
          ...(name === 'play_line' ? { revision: saved.revision, active_node_id: saved.activeNodeId }
            : { position: { kind: 'variation', node_id: saved.activeNodeId } }), ...extra,
        } } },
      })).json();
      assert.equal(result.result.isError, false, JSON.stringify(result));
      return result.result.structuredContent;
    }
    await tool('play_line', { moves: ['b8c6'] });
    await response.getByRole('button', { name: '2... Nc6', exact: true }).waitFor();
    await page.waitForTimeout(100);
    assert.ok(Math.abs(await history.evaluate(el => el.scrollTop) - reading) < 3, 'reading survives a demonstrated line');
    const boardStatus = page.locator('[aria-label="Game navigation"] output');
    assert.match(await boardStatus.innerText(), /4 of 4/, 'scrolling back did not suspend board following');
    const evidence = await tool('evaluate_position');
    await page.waitForFunction(reason => document.querySelector('[aria-label="Discussion history"]').textContent.includes(reason), evidence.reason);
    await page.waitForTimeout(100);
    assert.ok(Math.abs(await history.evaluate(el => el.scrollTop) - reading) < 3, 'reading survives evidence');
    await page.getByRole('button', { name: '← Previous', exact: true }).click();
    assert.ok(Math.abs(await history.evaluate(el => el.scrollTop) - reading) < 3, 'board navigation does not jump history');
    await page.getByRole('tab', { name: 'Analysis', exact: true }).click();
    await chunk(3);
    await page.getByRole('tab', { name: 'Trainer', exact: true }).click();
    await page.waitForTimeout(100);
    assert.ok(Math.abs(await history.evaluate(el => el.scrollTop) - reading) < 3, 'old reading survives text replacement');
    if (!returnLatest) {
      await history.evaluate(el => { el.scrollTop = el.scrollHeight - el.clientHeight - 100; });
      await page.waitForTimeout(100);
      reading = await history.evaluate(el => el.scrollTop);
      height += 200;
      await page.setViewportSize({ width: 1280, height });
      await page.waitForTimeout(100);
      assert.ok(await jump.isVisible(), 'window growth never implicitly resumes following');
    }
    const selectedPosition = await boardStatus.innerText();
    if (returnLatest) {
      await jump.click();
      assert.equal(await boardStatus.innerText(), selectedPosition, 'jump latest does not change the board');
      assert.ok(await tail() < 3, 'explicit return reaches latest content');
    }
    await tool('play_line', { moves: ['f1b5'] });
    await page.waitForFunction(() => document.querySelector('[aria-label="Discussion history"]').textContent.includes('Bb5'));
    assert.equal(await boardStatus.innerText(), selectedPosition, 'scroll following never resumes board following');
    await chunk(4);
    await page.waitForFunction(() => !document.querySelector('button[type="submit"]').disabled);
    if (returnLatest) {
      assert.ok(await tail() < 3, 'explicit return resumes following through completion');
    } else {
      assert.ok(Math.abs(await history.evaluate(el => el.scrollTop) - reading) < 3, 'completion preserves old reading');
      await jump.click();
      assert.ok(await tail() < 3, 'explicit return also works after completion');
    }
    const final = await (await page.request.get(`${endpoint}/0`)).json();
    assert.equal(final.attempts.at(-1).status, 'completed');
    assert.equal(await page.getByRole('region', { name: 'Trainer response', exact: true }).count(), 2);
    for (const locator of [input, page.getByLabel('Game position', { exact: true })]) {
      const box = await locator.boundingBox();
      assert.ok(box.y >= 0 && box.y + box.height <= height, 'board and composer stay accessible');
    }
    assert.equal(await response.evaluate(el => [...el.querySelectorAll('*')].some(child =>
      ['auto', 'scroll'].includes(getComputedStyle(child).overflowY) && child.scrollHeight > child.clientHeight
    )), false, 'long responses have no nested scrollers');
    assert.deepEqual(errors, []);
  } finally {
    try { await page.screenshot({ path: `${process.argv[3]}/scroll.png` }); }
    finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
