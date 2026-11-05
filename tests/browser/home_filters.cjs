const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  // Home filtering and the server-rendered Analysis link need no external CDN.
  await page.route('https://**/*', route => route.abort());
  const checkbox = name => page.getByRole('checkbox', { name, exact: true });
  const selections = () => new URL(page.url()).searchParams.getAll('result').sort();
  const games = () => page.getByRole('link').filter({ hasText: /^\s*vs Opponent/ }).allTextContents();
  const assertGames = async expected => {
    const content = await games();
    assert.deepEqual(content.map(text => Number(text.match(/Opponent (\d)/)[1])), expected);
    await page.getByText(`${expected.length} de 4 partidas`, { exact: true }).waitFor();
  };
  const toggle = async (name, checked, expected) => {
    await checkbox(name).setChecked(checked);
    await page.waitForURL(url => JSON.stringify(url.searchParams.getAll('result').sort()) === JSON.stringify(expected));
    assert.equal(await checkbox(name).isChecked(), checked);
  };
  try {
    await page.goto(process.argv[2]);
    await assertGames([3, 2, 1, 0]);
    await toggle('Derrota', true, ['loss']);
    await assertGames([3, 1]);
    await toggle('Empate', true, ['draw', 'loss']);
    await assertGames([3, 2, 1]);
    const filteredURL = page.url();
    await page.getByRole('link').filter({ hasText: /^\s*vs Opponent 3/ }).click();
    await page.waitForURL(url => url.pathname === '/analyses/lichess/result-3');
    await page.goBack();
    assert.equal(page.url(), filteredURL);
    assert.equal(await checkbox('Derrota').isChecked(), true);
    assert.equal(await checkbox('Empate').isChecked(), true);
    await assertGames([3, 2, 1]);
    // The explicit Home link preserves context too, not just browser Back.
    await page.getByRole('link').filter({ hasText: /^\s*vs Opponent 3/ }).click();
    await page.getByRole('link', { name: '← Home', exact: true }).click();
    assert.equal(page.url(), filteredURL);
    await assertGames([3, 2, 1]);
    await page.reload();
    assert.deepEqual(selections(), ['draw', 'loss']);
    assert.equal(await checkbox('Empate').isChecked(), true);
    await toggle('Derrota', false, ['draw']);
    await assertGames([2]);
    await toggle('Empate', false, []);
    await assertGames([3, 2, 1, 0]);
    assert.equal(await page.getByRole('link', { name: 'Limpar filtros' }).count(), 0);
    // Loading a bookmarked multi-selection restores both checkboxes and Games.
    await page.goto(filteredURL);
    assert.equal(await checkbox('Empate').isChecked(), true);
    assert.equal(await checkbox('Derrota').isChecked(), true);
    await assertGames([3, 2, 1]);
    await page.getByRole('link', { name: 'Limpar filtros', exact: true }).click();
    await page.waitForURL(url => url.pathname === '/' && !url.search);
    await assertGames([3, 2, 1, 0]);
    for (const name of ['Vitória', 'Empate', 'Derrota']) {
      assert.equal(await checkbox(name).isChecked(), false);
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
