const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: Number(process.argv[3]) } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const box = async locator => {
    const result = await locator.boundingBox();
    assert.ok(result, 'Element must be rendered');
    return result;
  };
  const visibleInViewport = async locator => {
    const r = await box(locator);
    assert.ok(r.x >= 0 && r.y >= 0 && r.x + r.width <= 1281 &&
      r.y + r.height <= Number(process.argv[3]) + 1, JSON.stringify(r));
    return r;
  };
  const scroll = async (locator, top) => {
    await locator.evaluate((el, value) => { el.scrollTop = value; }, top);
    await page.waitForTimeout(100);
    return locator.evaluate(el => el.scrollTop);
  };
  try {
    const fragmentLoaded = page.waitForResponse(response => response.url().includes('/0/fragment') && response.ok());
    await page.goto(`${process.argv[2]}/analyses/lichess/q7ZvsdUF`);
    await fragmentLoaded;
    // This waits for the real JS modules and Chessground, not just server HTML.
    await page.getByRole('tab', { name: 'Analysis', exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('[role="tab"]').getAttribute('aria-selected') === 'true');
    const board = page.getByLabel('Game position', { exact: true });
    const analysis = page.getByRole('tabpanel', { name: 'Analysis', exact: true });
    const trainer = page.getByRole('tab', { name: 'Trainer', exact: true });
    const analysisTab = page.getByRole('tab', { name: 'Analysis', exact: true });
    const originalBoard = await visibleInViewport(board);
    assert.ok(originalBoard.width >= 240, 'Board remains usable at short desktop heights');
    assert.ok(Math.abs(originalBoard.width - originalBoard.height) < 1, 'Square board');
    for (const name of ['Next →', '← Previous', 'Game', 'Discussion', 'Show Best Line', 'Mostrar linha prática']) {
      await visibleInViewport(page.getByRole('button', { name, exact: true }));
    }
    const analysisScroll = await scroll(analysis, 400);
    assert.ok(analysisScroll > 0, 'Critical Moments have their own scroll');
    assert.deepEqual(await box(board), originalBoard);
    await trainer.click();
    const history = page.getByRole('region', { name: 'Discussion history' });
    const composer = page.getByRole('textbox', { name: 'Discuss this Position' });
    const composerBox = await visibleInViewport(composer);
    await composer.fill('My next question');
    const historyScroll = await scroll(history, 500);
    assert.ok(historyScroll > 0, 'Discussion history has its own scroll');
    assert.deepEqual(await box(composer), composerBox);
    assert.deepEqual(await box(board), originalBoard);
    await visibleInViewport(page.getByRole('button', { name: 'Send to Trainer' }));
    assert.equal(await page.evaluate(() => window.scrollY), 0);
    // Navigate to an earlier Variation Position, then toggle tabs. Neither tab
    // should silently select the active end of the Discussion again.
    await page.getByRole('button', { name: 'Discussion', exact: true }).click();
    await page.getByRole('button', { name: '← Previous', exact: true }).click();
    const position = await page.getByRole('status').first().textContent();
    await analysisTab.click();
    assert.equal(await analysis.evaluate(el => el.scrollTop), analysisScroll);
    await trainer.click();
    assert.equal(await page.getByRole('status').first().textContent(), position);
    assert.equal(await history.evaluate(el => el.scrollTop), historyScroll);
    assert.equal(await composer.inputValue(), 'My next question');
    // Long explanations flow in the history, with no nested vertical scrollers.
    assert.equal(await history.evaluate(el => [...el.querySelectorAll('*')].some(child => {
      const style = getComputedStyle(child);
      return ['auto', 'scroll'].includes(style.overflowY) && child.scrollHeight > child.clientHeight;
    })), false);
    await scroll(history, 1e7);
    await page.getByText('End of the controlled explanation.', { exact: false }).waitFor();
    await visibleInViewport(composer);
    await page.getByText('Board details', { exact: true }).click();
    await page.getByRole('group', { name: 'Evaluation curve' }).waitFor();
    assert.deepEqual(await box(board), originalBoard);
    assert.deepEqual(errors, [], 'No browser runtime errors');
    await page.screenshot({ path: `${process.argv[4]}/desktop-${process.argv[3]}.png` });
  } catch (error) {
    await page.screenshot({ path: `${process.argv[4]}/failure-${process.argv[3]}.png` });
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
