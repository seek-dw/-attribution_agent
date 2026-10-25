// 管理员页面截图（配置 / 日志 / 计数器）
// 只读页面，不改数据。可用 BASE 环境变量指向其他实例。
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8123';

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 950 },
    deviceScaleFactor: 2,
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
  page.on('console', m => {
    if (m.type() === 'error' && !/404|last-result/.test(m.text())) {
      errors.push(m.text());
    }
  });

  // 登录用 fetch + location.href，不是表单提交，不能等 waitForNavigation
  await page.goto(BASE + '/auth/login', { waitUntil: 'domcontentloaded' });
  await page.fill('#username', 'admin');
  await page.fill('#password', 'admin123');
  await page.click('button[type=submit]');
  await page.waitForURL('**/workbench', { timeout: 20000 });
  await page.waitForTimeout(1500);
  console.log('当前 URL:', page.url());

  for (const [view, file] of [
    ['config', '08-config.png'],
    ['logs', '09-logs.png'],
    ['counters', '10-counters.png'],
  ]) {
    const btn = await page.$(`#nav button[data-view=${view}]`);
    if (!btn) { console.log(`✗ 找不到 ${view} 导航按钮`); continue; }
    await btn.click();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: 'shots/' + file });
    console.log('✓ ' + file);
  }

  // 统计页面元素，确认不是空白
  await page.click('#nav button[data-view=config]');
  await page.waitForTimeout(600);
  const cfg = await page.evaluate(() =>
    document.querySelectorAll('#configGrid .cfg-item').length);
  await page.click('#nav button[data-view=logs]');
  await page.waitForTimeout(600);
  const logs = await page.evaluate(() =>
    document.querySelectorAll('#logBody tr').length);
  await page.click('#nav button[data-view=counters]');
  await page.waitForTimeout(600);
  const counters = await page.evaluate(() =>
    document.querySelectorAll('#counterGrid .counter').length);

  console.log('\n元素统计: 配置项=%d 日志行=%d 计数器=%d', cfg, logs, counters);
  if (errors.length) {
    console.log('\n⚠ JS 错误:');
    errors.slice(0, 8).forEach(e => console.log('  -', e));
  } else {
    console.log('\n✓ 无 JS 错误');
  }
  await browser.close();
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
