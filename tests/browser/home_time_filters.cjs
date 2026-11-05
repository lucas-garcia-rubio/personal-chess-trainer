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
  await page.route('https://**/*', route => route.abort());
  const checkbox = name => page.getByRole('checkbox', { name, exact: true });
  const selections = parameter => new URL(page.url()).searchParams.getAll(parameter).sort();
  const assertGames = async expected => {
    const links = await page.getByRole('link').filter({ hasText: /^\s*vs Clock Opponent/ }).allTextContents();
    assert.deepEqual(links.map(text => Number(text.match(/Opponent (\d+)/)[1])), expected);
    await page.getByText(`${expected.length} de 13 partidas`, { exact: true }).waitFor();
  };
  const toggle = async (name, checked, parameter, expected) => {
    await checkbox(name).setChecked(checked);
    await page.waitForURL(url => JSON.stringify(url.searchParams.getAll(parameter).sort()) === JSON.stringify(expected));
    assert.equal(await checkbox(name).isChecked(), checked);
  };
  try {
    await page.goto(process.argv[2]);
    await assertGames([12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0]);
    await toggle('10+0', true, 'time_control', ['600+0']);
    await assertGames([5, 2, 0]);
    await toggle('10+5', true, 'time_control', ['600+0', '600+5']);
    await assertGames([5, 4, 2, 1, 0]);
    await toggle('Derrota', true, 'result', ['loss']);
    await assertGames([4, 0]);
    await toggle('Empate', true, 'result', ['draw', 'loss']);
    await assertGames([5, 4, 1, 0]);
    const filteredURL = page.url();
    const openAnalysis = () => page.getByRole('link').filter({ hasText: /^\s*vs Clock Opponent 5/ }).click();
    const assertSelection = async () => {
      assert.deepEqual(selections('result'), ['draw', 'loss']);
      assert.deepEqual(selections('time_control'), ['600+0', '600+5']);
      for (const name of ['10+0', '10+5', 'Empate', 'Derrota']) {
        assert.equal(await checkbox(name).isChecked(), true);
      }
      await assertGames([5, 4, 1, 0]);
    };
    await openAnalysis();
    await page.waitForURL(url => url.pathname === '/analyses/lichess/time-5');
    await page.goBack();
    assert.equal(page.url(), filteredURL);
    await assertSelection();
    await openAnalysis();
    await page.getByRole('link', { name: '← Home', exact: true }).click();
    assert.equal(page.url(), filteredURL);
    await assertSelection();
    await page.goto(filteredURL);
    await assertSelection();
    await toggle('10+0', false, 'time_control', ['600+5']);
    await assertGames([4, 1]);
    await toggle('Derrota', false, 'result', ['draw']);
    await toggle('Empate', false, 'result', []);
    await toggle('Vitória', true, 'result', ['win']);
    await assertGames([]);
    await page.getByText('Nenhuma partida encontrada', { exact: true }).waitFor();
    for (const name of ['10+0', '10+5', '3+0', '40/7200:3600']) {
      assert.equal(await checkbox(name).count(), 1);
    }
    await page.getByRole('link', { name: 'Limpar filtros', exact: true }).click();
    await page.waitForURL(url => !url.search);
    await assertGames([12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0]);
    assert.equal(await page.getByRole('checkbox', { checked: true }).count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
