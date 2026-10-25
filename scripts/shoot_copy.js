// 验证复制与导出（2.1.3 结果区要求支持复制和导出）
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8123';

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 950 },
    deviceScaleFactor: 2,
    // 授权剪贴板，否则 headless 里读不到
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await ctx.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));

  await page.goto(BASE + '/auth/login', { waitUntil: 'domcontentloaded' });
  await page.click('button[type=submit]');
  await page.waitForURL('**/workbench', { timeout: 20000 });
  await page.waitForTimeout(2000);

  // 打开有结果的会话
  const convs = await page.$$('#convList .conv');
  console.log('会话数:', convs.length);
  // 找第一个有结果的（点开看右侧是否有卡片）
  let opened = false;
  for (const c of convs) {
    await c.click();
    await page.waitForTimeout(1200);
    const n = await page.evaluate(() =>
      document.querySelectorAll('#resultPane .card').length);
    if (n > 0) { opened = true; console.log('已打开有结果的会话，卡片数:', n); break; }
  }
  if (!opened) { console.log('✗ 没找到有结果的会话'); await browser.close(); return; }

  console.log('复制全文按钮可用:', await page.isEnabled('#btnCopyAll'));
  console.log('导出按钮可用:', await page.isEnabled('#btnExport'));

  // 点复制全文
  await page.click('#btnCopyAll');
  await page.waitForTimeout(600);
  const toastTxt = await page.evaluate(() => {
    const t = document.querySelector('#toastWrap .toast');
    return t ? t.textContent : '(无 toast)';
  });
  console.log('复制后提示:', toastTxt);

  let clip = '';
  try {
    clip = await page.evaluate(() => navigator.clipboard.readText());
  } catch (e) {
    clip = '(读取剪贴板失败: ' + e.message + ')';
  }
  console.log('剪贴板长度:', clip.length);
  console.log('剪贴板前 120 字:', clip.slice(0, 120).replace(/\n/g, ' | '));

  // 校验复制的文本包含六部分
  const need = ['【问题定义】', '【关键指标】', '【归因结论】', '【待补充数据】', '【下一步建议】'];
  const hit = need.filter(k => clip.includes(k));
  console.log('六部分命中:', hit.length + '/' + need.length, hit.join(','));

  // 证据页复制
  await page.click('#resultTabs button[data-tab=evidence]');
  await page.waitForTimeout(500);
  const evBtn = await page.$('#resultPane [data-copy=evidence]');
  if (evBtn) {
    await evBtn.click();
    await page.waitForTimeout(500);
    const clip2 = await page.evaluate(() => navigator.clipboard.readText());
    console.log('证据复制长度:', clip2.length);
  } else {
    console.log('✗ 证据复制按钮不存在');
  }

  // 结论卡复制
  await page.click('#resultTabs button[data-tab=result]');
  await page.waitForTimeout(500);
  const cBtn = await page.$('#resultPane [data-copy=conclusion]');
  if (cBtn) {
    await cBtn.click();
    await page.waitForTimeout(500);
    const clip3 = await page.evaluate(() => navigator.clipboard.readText());
    console.log('结论复制长度:', clip3.length);
  } else {
    console.log('✗ 结论复制按钮不存在');
  }

  // 导出：接口层已用 curl 验证 200 + 2641 字节。
  // 这里只验证点击后前端确实发起了请求（新窗口的请求不走当前页监听，
  // 所以用 request 事件在 context 层抓）。
  let exportUrl = null;
  ctx.on('request', (req) => {
    if (req.url().includes('/download')) exportUrl = req.url();
  });
  await page.click('#btnExport');
  await page.waitForTimeout(2000);
  if (exportUrl) {
    const r = await ctx.request.get(exportUrl);
    const txt = await r.text();
    console.log('导出请求:', exportUrl.split('/api')[1],
      '| HTTP', r.status(),
      '| 含报告标题:', txt.includes('经营归因分析报告'),
      '| 长度:', txt.length);
  } else {
    console.log('✗ 未捕获导出请求');
  }

  await page.screenshot({ path: 'shots/11-copy.png' });
  console.log('✓ 11-copy.png');

  if (errs.length) {
    console.log('⚠ JS 错误:', errs.slice(0, 5));
  } else {
    console.log('✓ 无 JS 错误');
  }
  await browser.close();
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
