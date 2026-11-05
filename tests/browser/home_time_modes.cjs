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
    const links = await page.getByRole('link').filter({ hasText: /^\s*vs Category Opponent/ }).allTextContents();
    assert.deepEqual(links.map(text => Number(text.match(/Opponent (\d+)/)[1])), expected);
    await page.getByText(`${expected.length} de 9 partidas`, { exact: true }).waitFor();
  };
  const toggle = async (name, checked, parameter, expected) => {
    await checkbox(name).setChecked(checked);
    await page.waitForURL(url => JSON.stringify(url.searchParams.getAll(parameter).sort()) === JSON.stringify(expected));
    assert.equal(await checkbox(name).isChecked(), checked);
  };
  const assertMode = async mode => {
    const category = mode === 'category';
    assert.equal(await page.getByRole('tab', { name: 'Categoria', exact: true }).getAttribute('aria-selected'), String(category));
    assert.equal(await page.getByRole('tab', { name: 'Controle exato', exact: true }).getAttribute('aria-selected'), String(!category));
    assert.equal(await checkbox('Blitz').count(), category ? 1 : 0);
    assert.equal(await checkbox('10+0').count(), category ? 0 : 1);
    const active = page.getByRole('tab', { selected: true });
    const inactive = page.getByRole('tab', { selected: false });
    assert.notEqual(await active.evaluate(element => getComputedStyle(element).backgroundColor),
                    await inactive.evaluate(element => getComputedStyle(element).backgroundColor));
    await page.getByText('Trocar o modo limpa a seleção de tempo', { exact: true }).waitFor();
  };
  const switchMode = async mode => {
    await page.getByRole('tab', { name: mode === 'category' ? 'Categoria' : 'Controle exato', exact: true }).click();
    await page.waitForURL(url => url.searchParams.get('time_mode') === mode);
    await assertMode(mode);
    assert.deepEqual(selections('category'), []);
    assert.deepEqual(selections('time_control'), []);
    assert.equal(await page.locator('input[name="category"]:checked, input[name="time_control"]:checked').count(), 0);
  };
  try {
    await page.goto(process.argv[2]);
    await assertMode('exact');
    await toggle('10+0', true, 'time_control', ['600+0']);
    await toggle('Derrota', true, 'result', ['loss']);
    await switchMode('category');
    assert.deepEqual(selections('result'), ['loss']);
    await assertGames([8, 5, 3, 0]);
    await toggle('Blitz', true, 'category', ['blitz']);
    await assertGames([0]);
    await toggle('Rapid', true, 'category', ['blitz', 'rapid']);
    await toggle('Empate', true, 'result', ['draw', 'loss']);
    await assertGames([1, 0]);
    const filteredURL = page.url();
    const assertSelection = async () => {
      await assertMode('category');
      assert.deepEqual(selections('result'), ['draw', 'loss']);
      assert.deepEqual(selections('category'), ['blitz', 'rapid']);
      for (const name of ['Blitz', 'Rapid', 'Empate', 'Derrota']) assert.equal(await checkbox(name).isChecked(), true);
      await assertGames([1, 0]);
    };
    const openAnalysis = () => page.getByRole('link').filter({ hasText: /^\s*vs Category Opponent 1/ }).click();
    await openAnalysis();
    await page.waitForURL(url => url.pathname === '/analyses/lichess/category-1');
    await page.goBack();
    assert.equal(page.url(), filteredURL);
    await assertSelection();
    await openAnalysis();
    await page.getByRole('link', { name: '← Home', exact: true }).click();
    assert.equal(page.url(), filteredURL);
    await assertSelection();
    await page.goto(filteredURL);
    await assertSelection();
    await switchMode('exact');
    assert.deepEqual(selections('result'), ['draw', 'loss']);
    await assertGames([8, 7, 5, 4, 3, 1, 0]);
    await toggle('10+5', true, 'time_control', ['600+5']);
    await assertGames([7, 5, 1]);
    await switchMode('category');
    assert.deepEqual(selections('result'), ['draw', 'loss']);
    await toggle('Bullet', true, 'category', ['bullet']);
    await toggle('Derrota', false, 'result', ['draw']);
    await assertGames([]);
    await page.getByText('Nenhuma partida encontrada', { exact: true }).waitFor();
    for (const name of ['Blitz', 'Rapid', 'Bullet', 'UltraBullet', 'Classical', 'Correspondence']) {
      assert.equal(await checkbox(name).count(), 1);
    }
    assert.equal(await checkbox('Desconhecida').count(), 2);
    await toggle('Bullet', false, 'category', []);
    await assertGames([7, 4, 1]);
    await page.getByRole('link', { name: 'Limpar filtros', exact: true }).click();
    await page.waitForURL(url => !url.search);
    await assertGames([8, 7, 6, 5, 4, 3, 2, 1, 0]);
    await switchMode('category');
    assert.equal(await page.getByRole('link', { name: 'Limpar filtros', exact: true }).count(), 0);
    await assertGames([8, 7, 6, 5, 4, 3, 2, 1, 0]);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
