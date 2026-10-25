// 截图验证前端实际渲染效果
// 注意：会创建会话与消息。请对独立数据目录的服务截图，避免污染演示数据：
//   ATTR_DATA_DIR=./data_shot python -m uvicorn app.main:app --port 8126
//   BASE=http://127.0.0.1:8126 node scripts/shoot.js
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
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

  // 1. 登录页
  await page.goto(BASE + '/auth/login', { waitUntil: 'networkidle' });
  await page.waitForTimeout(500);
  await page.screenshot({ path: 'shots/01-login.png' });
  console.log('✓ 01-login.png');

  // 登录
  await page.click('button[type=submit]');
  await page.waitForURL('**/workbench', { timeout: 15000 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: 'shots/02-workbench-empty.png' });
  console.log('✓ 02-workbench-empty.png');

  // 优先复用已有会话，避免每次截图都新建（会污染演示数据）
  const convCount = await page.evaluate(() =>
    document.querySelectorAll('#convList .conv').length);
  if (convCount > 0) {
    console.log(`复用已有 ${convCount} 个会话，不新建`);
  } else {
    await page.click('#btnNewConv');
    await page.waitForTimeout(1200);
  }

  // 3. 提问（缺条件 → 澄清）
  await page.fill('#input', '销售额');
  await page.click('#btnSend');
  await page.waitForTimeout(6000);
  await page.screenshot({ path: 'shots/03-clarify.png' });
  console.log('✓ 03-clarify.png');

  // 4. 完整因果提问
  await page.fill('#input', '为什么 2024-06 的销售额下滑了？');
  await page.click('#btnSend');
  // 等任务完成
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(500);
    const hidden = await page.evaluate(() =>
      document.getElementById('runbar').style.display === 'none');
    if (hidden) break;
  }
  await page.waitForTimeout(2000);
  await page.screenshot({ path: 'shots/04-result.png' });
  console.log('✓ 04-result.png');

  // 5. 证据 tab
  await page.click('#resultTabs button[data-tab=evidence]');
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'shots/05-evidence.png' });
  console.log('✓ 05-evidence.png');

  // 6. 轨迹 tab
  await page.click('#resultTabs button[data-tab=trace]');
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'shots/06-trace.png' });
  console.log('✓ 06-trace.png');

  // 7. 附件 tab
  await page.click('#resultTabs button[data-tab=files]');
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'shots/07-files.png' });
  console.log('✓ 07-files.png');

  // 检查结果区是否真的有内容
  await page.click('#resultTabs button[data-tab=result]');
  await page.waitForTimeout(400);
  const stats = await page.evaluate(() => ({
    cards: document.querySelectorAll('#resultPane .card').length,
    hyps: document.querySelectorAll('#resultPane .hyp').length,
    metrics: document.querySelectorAll('#resultPane table.metrics tbody tr').length,
    chatMsgs: document.querySelectorAll('#chat .msg').length,
    concl: (document.querySelector('#resultPane .conclusion') || {}).textContent || '',
  }));
  console.log('\n渲染统计:', JSON.stringify(stats, null, 2));

  // 8. 管理员视图
  await ctx.clearCookies();
  await page.goto(BASE + '/auth/login', { waitUntil: 'networkidle' });
  await page.fill('#username', 'admin');
  await page.fill('#password', 'admin123');
  await page.click('button[type=submit]');
  await page.waitForURL('**/workbench', { timeout: 15000 });
  await page.waitForTimeout(1000);

  await page.click('#nav button[data-view=config]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: 'shots/08-config.png' });
  console.log('✓ 08-config.png');

  await page.click('#nav button[data-view=logs]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: 'shots/09-logs.png' });
  console.log('✓ 09-logs.png');

  await page.click('#nav button[data-view=counters]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: 'shots/10-counters.png' });
  console.log('✓ 10-counters.png');

  if (errors.length) {
    console.log('\n⚠ 控制台错误:');
    errors.slice(0, 10).forEach(e => console.log('  -', e));
  } else {
    console.log('\n✓ 无控制台错误');
  }

  await browser.close();
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
