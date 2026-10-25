// 数据源页与用户页的真实点击验证 + 截图
// 使用隔离数据目录的实例：ATTR_DATA_DIR=./data_shot ... --port 8126
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.BASE || 'http://127.0.0.1:8123';
const OUT = 'shots';

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 950 },
    deviceScaleFactor: 2,
  });
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));
  page.on('console', m => {
    if (m.type() === 'error' && !/404|last-result/.test(m.text())) {
      errs.push(m.text());
    }
  });

  // 管理员登录
  await page.goto(BASE + '/auth/login', { waitUntil: 'domcontentloaded' });
  await page.fill('#username', 'admin');
  await page.fill('#password', 'admin123');
  await page.click('button[type=submit]');
  await page.waitForURL('**/workbench', { timeout: 20000 });
  await page.waitForTimeout(1200);

  /* ---------------- 数据源页 ---------------- */
  await page.click('#nav button[data-view=sources]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(OUT, '12-sources-empty.png') });
  console.log('✓ 12-sources-empty.png（空状态）');

  // 点「登记数据源」，填表，保存
  await page.click('#btnAddSource');
  await page.waitForTimeout(400);
  const dbPath = path.resolve('data', 'demo_source.db');
  await page.fill('#dsName', '演示库（页面登记）');
  await page.fill('#dsPath', dbPath);
  await page.screenshot({ path: path.join(OUT, '13-sources-form.png') });
  console.log('✓ 13-sources-form.png（填表）');

  await page.click('#btnSaveSource');
  await page.waitForTimeout(2000);

  const afterSave = await page.evaluate(() => {
    const cards = document.querySelectorAll('#sourceList .card');
    const txt = (cards[0] || {}).textContent || '';
    return { n: cards.length, text: txt.slice(0, 200) };
  });
  console.log('登记后卡片数:', afterSave.n);
  console.log('第一张卡内容:', afterSave.text.replace(/\s+/g, ' ').slice(0, 160));
  if (afterSave.n === 0) {
    console.log('✗ 页面上没有出现数据源卡片');
  }

  // 发现「第一个数据源自动激活」→ 会读这个空库！必须测完就切回
  // 点探查结构
  const inspectBtn = await page.$('#sourceList [data-act=inspect]');
  if (inspectBtn) {
    await inspectBtn.click();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(OUT, '14-sources-inspect.png') });
    const inspect = await page.evaluate(() => {
      const c = document.querySelector('#sourceInspect .card');
      return c ? c.textContent.replace(/\s+/g, ' ').slice(0, 300) : '(无)';
    });
    console.log('✓ 14-sources-inspect.png');
    console.log('探查结果:', inspect.slice(0, 220));
  } else {
    console.log('✗ 找不到探查按钮');
  }

  // 点测试连通
  const testBtn = await page.$('#sourceList [data-act=test]');
  if (testBtn) {
    await testBtn.click();
    await page.waitForTimeout(1200);
    const toastTxt = await page.evaluate(() => {
      const t = document.querySelector('#toastWrap .toast');
      return t ? t.textContent : '(无 toast)';
    });
    console.log('测试连通提示:', toastTxt);
  }

  // 删除，清理
  page.on('dialog', d => d.accept());
  const delBtn = await page.$('#sourceList [data-act=del]');
  if (delBtn) {
    await delBtn.click();
    await page.waitForTimeout(1200);
    const n = await page.evaluate(() =>
      document.querySelectorAll('#sourceList .card').length);
    console.log('删除后卡片数:', n);
  } else {
    console.log('注意：没有删除按钮（该源可能被自动激活）');
  }

  /* ---------------- 用户页 ---------------- */
  await page.click('#nav button[data-view=users]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(OUT, '15-users.png') });
  const users = await page.evaluate(() =>
    document.querySelectorAll('#userList tbody tr').length);
  console.log('✓ 15-users.png（用户数=%d）', users);

  // 新建用户
  await page.click('#btnAddUser');
  await page.waitForTimeout(400);
  const uname = 'demo' + Date.now().toString().slice(-6);
  await page.fill('#nuName', uname);
  await page.fill('#nuDisplay', '演示用户');
  await page.fill('#nuPwd', 'demo123456');
  await page.click('#btnSaveUser');
  await page.waitForTimeout(1500);
  const users2 = await page.evaluate(() =>
    document.querySelectorAll('#userList tbody tr').length);
  console.log('新建后用户数:', users2, users2 === users + 1 ? '✓' : '✗');

  await page.screenshot({ path: path.join(OUT, '16-users-created.png') });
  console.log('✓ 16-users-created.png');

  /* ---------------- 功能开关 ---------------- */
  await page.click('#nav button[data-view=config]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(OUT, '17-config-switches.png') });
  const sw = await page.evaluate(() =>
    document.querySelectorAll('#configGrid input[data-sw]').length);
  console.log('✓ 17-config-switches.png（开关数=%d）', sw);

  if (errs.length) {
    console.log('\n⚠ JS 错误:');
    errs.slice(0, 6).forEach(e => console.log('  -', e));
  } else {
    console.log('\n✓ 无 JS 错误');
  }
  await browser.close();
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
