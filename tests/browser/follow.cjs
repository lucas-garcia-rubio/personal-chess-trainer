const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 800}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = process.argv[2];
    const url = `${base}/analyses/lichess/q7ZvsdUF/discussions`;
    const saved = async () => (await page.request.get(`${url}/0`)).json();
    await page.goto(`${base}/analyses/lichess/q7ZvsdUF?trainer=1`);
    const oldMove = page.getByRole('navigation', {name: 'Discussion Variations'}).getByRole('button', {name: '1. e4 · Player', exact: true});
    await oldMove.waitFor();
    const original = await saved();
    const selectionWrites = [];
    page.on('request', request => {
      if (request.url().endsWith('/selection') && request.method() === 'POST') selectionWrites.push(request.url());
    });
    await page.getByRole('textbox', {name: 'Discuss this Position'}).fill('Demonstrate more');
    await page.getByRole('button', {name: 'Send to Trainer', exact: true}).click();
    await page.getByText('Inspect while I demonstrate.', {exact: true}).waitFor();
    const advance = async (moves) => {
      const discussion = await saved();
      const {token} = await (await page.request.get(`${base}/test/runtime`)).json();
      const response = await page.request.post(`${base}/trainer/mcp`, {headers: {Authorization: `Bearer ${token}`}, data: {
        jsonrpc: '2.0', id: 1, method: 'tools/call', params: {name: 'play_line', arguments: {
          attempt_id: discussion.attempts.at(-1).id, revision: discussion.revision,
          active_node_id: discussion.activeNodeId, moves,
        }},
      }});
      assert.equal((await response.json()).result.isError, false);
    };
    const boardStatus = page.getByRole('status').filter({hasText: /^Discussion ·/});
    await advance(['b8c6']);
    await page.getByRole('button', {name: '2... Nc6', exact: true}).waitFor();
    await page.getByRole('status').filter({hasText: /^Discussion · 4 of 4$/}).waitFor();
    await oldMove.click();
    await page.getByText('Discutindo a posição após 1. e4', {exact: true}).waitFor();
    const inspectedStatus = await boardStatus.innerText();
    await advance(['f1b5']);
    await page.getByRole('button', {name: '3. Bb5', exact: true}).waitFor();
    assert.equal(await boardStatus.innerText(), inspectedStatus);
    assert.equal((await saved()).activeNodeId, (await saved()).nodes.at(-1).id);
    assert.equal(await oldMove.getAttribute('aria-pressed'), 'true');
    await page.getByRole('button', {name: 'Acompanhar Trainer', exact: true}).click();
    await page.getByRole('status').filter({hasText: /^Discussion · 5 of 5$/}).waitFor();
    await advance(['a7a6']);
    await page.getByRole('status').filter({hasText: /^Discussion · 6 of 6$/}).waitFor();
    const history = page.getByRole('region', {name: 'Discussion history'});
    await history.evaluate(element => { element.scrollTop = 0; });
    const readingPosition = await history.evaluate(element => element.scrollTop);
    await page.getByRole('button', {name: 'Previous', exact: false}).click();
    await page.getByRole('status').filter({hasText: /^Discussion · 5 of 6$/}).waitFor();
    await page.getByText('Discutindo a posição após 3. Bb5', {exact: true}).waitFor();
    assert.equal(await history.evaluate(element => element.scrollTop), readingPosition);
    await advance(['b5a4']);
    await page.getByRole('button', {name: '4. Ba4', exact: true}).waitFor();
    assert.equal(await boardStatus.innerText(), 'Discussion · 5 of 6');
    await page.getByRole('button', {name: 'Next', exact: false}).click();
    await page.getByText('Discutindo a posição após 3... a6', {exact: true}).waitFor();
    assert.equal(await boardStatus.innerText(), 'Discussion · 6 of 6');
    await page.getByRole('button', {name: 'Acompanhar Trainer', exact: true}).click();
    await page.getByRole('status').filter({hasText: /^Discussion · 7 of 7$/}).waitFor();
    await page.getByRole('button', {name: 'Base Position', exact: true}).click();
    await page.getByText('Discutindo a Base Position', {exact: true}).waitFor();
    await advance(['g8f6']);
    await page.getByRole('button', {name: '4... Nf6', exact: true}).waitFor();
    assert.equal(await boardStatus.innerText(), 'Discussion · 0 of 0');
    await page.getByRole('button', {name: 'Game', exact: true}).click();
    await advance(['a4b3']);
    await page.getByRole('button', {name: '5. Bb3', exact: true}).waitFor();
    await page.getByRole('status').filter({hasText: /^Start$/}).waitFor();
    await page.getByRole('button', {name: 'Acompanhar Trainer', exact: true}).click();
    await page.getByRole('status').filter({hasText: /^Discussion · 9 of 9$/}).waitFor();
    await oldMove.click();
    await page.request.post(`${base}/test/release`);
    await page.getByText(/The demonstration is complete\./).waitFor();
    await page.getByText('Discutindo a posição após 1. e4', {exact: true}).waitFor();
    await page.getByRole('textbox', {name: 'Discuss this Position'}).fill('Explain the inspected Position');
    const sent = page.waitForResponse(r => r.url() === url && r.request().method() === 'POST');
    await page.getByRole('button', {name: 'Send to Trainer', exact: true}).click();
    await sent;
    let contexts;
    for (let n = 0; n < 100; n++) {
      contexts = (await (await page.request.get(`${base}/test/runtime`)).json()).contexts;
      if (contexts.length === 2) break;
      await page.waitForTimeout(20);
    }
    assert.equal(contexts.length, 2);
    assert.equal(contexts[1].active_node_id, original.nodes[0].id);
    assert.equal(contexts[1].active_position, original.nodes[0].fen);
    const final = await saved();
    assert.equal(final.baseFen, original.baseFen);
    assert.deepEqual(final.nodes.slice(0, 3), original.nodes);
    assert.deepEqual(selectionWrites, []);
    assert.deepEqual(errors, []);
  } finally { await fetch(`${process.argv[2]}/test/release`, {method: 'POST'}); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
