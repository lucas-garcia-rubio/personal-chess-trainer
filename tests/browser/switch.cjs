const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH});
  const base = process.argv[2];
  const ending = process.argv[3];
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 800}});
    // Hold public transport callbacks to exercise already-queued events after switching.
    await page.addInitScript(() => {
      const NativeSource = window.EventSource;
      window.testSources = [];
      window.EventSource = class extends NativeSource {
        constructor(url) {
          super(url);
          window.testSources.push(this);
          this.addEventListener('message', event => { this.firstMessage ||= event.data; });
        }
      };
    });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const url = `${base}/analyses/lichess/q7ZvsdUF/discussions`;
    const saved = async ply => (await page.request.get(`${url}/${ply}`)).json();
    const state = async () => (await page.request.get(`${base}/test/runtime`)).json();
    let connections = 0;
    let reconnectCursor;
    if (ending === 'completed') {
      await page.route(`${url}/0/attempts/*/events?*`, async route => {
        connections++;
        if (connections !== 1) {
          reconnectCursor = route.request().headers()['last-event-id'];
          return route.continue();
        }
        // End one real EventSource connection after a saved prefix; its native
        // retry must resume through the public endpoint without duplicate entries.
        const attempt = (await saved(0)).attempts.at(-1);
        const event = attempt.events.at(-1);
        const html = await (await page.request.get(`${url}/0/fragment?attempt_id=${attempt.id}&cursor=${event.sequence}`)).text();
        await route.fulfill({contentType: 'text/event-stream', body:
          `retry: 30\nid: ${event.sequence}\ndata: ${JSON.stringify({...event, html})}\n\n`});
      });
    }
    await page.goto(`${base}/analyses/lichess/q7ZvsdUF?trainer=1`);
    const input = page.getByRole('textbox', {name: 'Discuss this Position'});
    const send = page.getByRole('button', {name: 'Send to Trainer', exact: true});
    const history = page.getByRole('region', {name: 'Discussion history'});
    const boardStatus = page.locator('[aria-label="Game navigation"] output');
    await page.getByText('Earlier origin explanation.', {exact: true}).waitFor();
    await input.fill('Demonstrate at origin');
    await send.click();
    await page.getByText('Origin demonstration in progress.', {exact: true}).waitFor();
    const originAttempt = (await saved(0)).attempts.at(-1).id;
    const tool = async (name, extra) => {
      const d = await saved(0);
      const result = await (await page.request.post(`${base}/trainer/mcp`, {
        headers: {Authorization: `Bearer ${(await state()).token}`},
        data: {jsonrpc: '2.0', id: 1, method: 'tools/call', params: {name, arguments: {
          attempt_id: originAttempt, ...extra(d),
        }}},
      })).json();
      assert.equal(result.result.isError, false, JSON.stringify(result));
    };
    await tool('play_line', d => ({revision: d.revision, active_node_id: d.activeNodeId, moves: ['b8c6']}));
    await page.getByRole('button', {name: '2... Nc6', exact: true}).waitFor();
    await page.getByRole('navigation', {name: 'Discussion Variations'})
      .getByRole('button', {name: '1. e4 · Player', exact: true}).click();
    await page.getByText('Discutindo a posição após 1. e4', {exact: true}).waitFor();
    const inspected = await boardStatus.innerText();
    await page.evaluate(id => {
      const source = window.testSources.find(s => s.url.includes(`/attempts/${id}/`));
      source.onmessage(new MessageEvent('message', {data: source.firstMessage}));
    }, originAttempt);
    await page.waitForTimeout(100);
    assert.equal(await boardStatus.innerText(), inspected, 'active-connection replay cannot replace inspection');
    await page.getByText('Origin demonstration in progress.', {exact: true}).waitFor();
    if (ending === 'completed') {
      assert.ok(connections >= 2, 'native EventSource reconnected');
      assert.ok(Number(reconnectCursor) > 0, 'reconnection carries the processed cursor');
    }
    let destinationPly = 1;
    if (ending === 'failed') {
      await page.getByRole('tab', {name: 'Analysis', exact: true}).click();
      await page.getByRole('tabpanel', {name: 'Analysis', exact: true}).getByRole('button').first().click();
      destinationPly = Number((await boardStatus.innerText()).match(/^Ply (\d+)/)[1]);
      await page.getByRole('tab', {name: 'Trainer', exact: true}).click();
    } else {
      await page.getByRole('button', {name: 'Game', exact: true}).click();
      await page.getByRole('button', {name: 'Next →', exact: true}).click();
    }
    await input.fill('Explain destination');
    await send.click();
    await page.getByText('Destination explanation.', {exact: true}).waitFor();
    const destination = await saved(destinationPly);
    const destinationContext = (await state()).contexts.at(-1);
    assert.equal(destinationContext.base_ply, destinationPly);
    assert.equal(destinationContext.active_position, destination.baseFen);
    const destinationStatus = await boardStatus.innerText();
    await input.fill('Destination draft');
    await tool('play_line', d => ({revision: d.revision, active_node_id: d.activeNodeId, moves: ['f1b5']}));
    await tool('evaluate_position', () => ({position: {kind: 'game', ply: 0}}));
    assert.equal((await saved(0)).attempts.at(-1).status, 'running');
    assert.deepEqual((await state()).aborted, []);
    // A late disconnect from the origin must never display a destination failure.
    await page.evaluate(id => {
      const source = window.testSources.find(s => s.url.includes(`/attempts/${id}/`));
      source.onerror(new Event('error'));
    }, originAttempt);
    assert.equal(await page.getByText('Reconnecting to the saved Trainer response…', {exact: true}).isVisible(), false);
    if (ending === 'interrupted') {
      await page.request.post(`${url}/0/attempts/${originAttempt}/interrupt`);
    } else await page.request.post(`${base}/test/release`);
    await page.waitForFunction(async ({endpoint, ending}) => {
      const d = await (await fetch(endpoint)).json();
      return d.attempts.at(-1).status === ending;
    }, {endpoint: `${url}/0`, ending});
    assert.equal(await boardStatus.innerText(), destinationStatus);
    assert.equal(await input.inputValue(), 'Destination draft');
    assert.equal(await history.getByText(/Origin demonstration/).count(), 0);
    assert.equal(await history.getByRole('group', {name: 'Demonstrated line'}).count(), 0);
    assert.equal(await history.getByText('Ver avaliações', {exact: true}).count(), 0);
    assert.equal(await page.getByText(/Response (failed|interrupted)/).isVisible(), false);
    assert.deepEqual(await saved(destinationPly), destination);
    // Reopen origin explicitly; its inspected Position must survive background moves.
    await page.getByRole('button', {name: 'Game', exact: true}).click();
    for (let ply = destinationPly; ply > 0; ply--) {
      await page.getByRole('button', {name: '← Previous', exact: true}).click();
    }
    await page.getByRole('button', {name: 'Discussion', exact: true}).click();
    const originText = ending === 'completed' ? /Origin demonstration complete\./ : 'Origin demonstration in progress.';
    await page.getByText(originText, {exact: ending !== 'completed'}).waitFor();
    assert.equal(await boardStatus.innerText(), inspected);
    await page.getByText('Discutindo a posição após 1. e4', {exact: true}).waitFor();
    assert.equal(await history.getByRole('region', {name: 'Trainer response', exact: true}).count(), 2);
    assert.equal(await history.getByRole('button', {name: '3. Bb5', exact: true}).count(), 1);
    if (ending !== 'completed') {
      await page.getByText(ending === 'failed' ? 'Response failed. Your Discussion is saved.' : 'Response interrupted.', {exact: true}).waitFor();
      await page.getByRole('button', {name: 'Tentar novamente', exact: true}).click();
      await page.getByText('Origin retry explanation.', {exact: true}).waitFor();
      assert.equal(await history.getByRole('region', {name: 'Trainer response', exact: true}).count(), 3);
      await page.getByText('Discutindo a posição após 1. e4', {exact: true}).waitFor();
    }
    await page.getByRole('button', {name: 'Acompanhar Trainer', exact: true}).click();
    const resumedStatus = await boardStatus.innerText();
    if (ending === 'completed') assert.equal(resumedStatus, 'Discussion · 5 of 5');
    await page.evaluate(id => {
      const source = window.testSources.find(s => s.url.includes(`/attempts/${id}/`));
      source.onmessage(new MessageEvent('message', {data: source.firstMessage}));
      source.onerror(new Event('error'));
    }, originAttempt);
    // Let queued transport work finish before asserting the completed projection.
    await page.waitForTimeout(100);
    assert.equal(await history.getByText(originText, {exact: ending !== 'completed'}).count(), 1);
    assert.equal(await boardStatus.innerText(), resumedStatus);
    assert.equal(await send.isEnabled(), true);
    assert.equal(await page.getByText('Reconnecting to the saved Trainer response…', {exact: true}).isVisible(), false);
    assert.deepEqual(errors, []);
  } finally {
    await fetch(`${base}/test/release`, {method: 'POST'});
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
