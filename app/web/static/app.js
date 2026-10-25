/* ============================================================
   经营归因分析 Agent — 前端逻辑
   ============================================================ */
'use strict';

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const BODY = document.body;
const ME = {
  id: Number(BODY.dataset.userId),
  role: BODY.dataset.userRole,
  name: BODY.dataset.userName,
};

const S = {
  convId: null,
  convs: [],
  taskId: null,
  runningTask: null,
  lastResult: null,
  lastTaskId: null,
  ws: null,
  wsGen: 0,
  tab: 'result',
  attachments: [],
  trace: [],
  logs: [],
  config: {},
  counters: [],
  sources: [],
  users: [],
  view: 'workbench',
};

/* ---------------------------------------------------------- 工具函数 */
function toast(msg, kind) {
  const t = el('div', 'toast' + (kind ? ' ' + kind : ''), esc(msg));
  $('toastWrap').appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity .2s';
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 200);
  }, 2400);
}

async function api(url, opts = {}) {
  const r = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  if (r.status === 401) {
    location.href = '/auth/login';
    throw new Error('未登录');
  }
  if (!r.ok) {
    let d = null;
    try { d = await r.json(); } catch (_) {}
    const msg = (d && (d.detail || d.message)) || `请求失败（${r.status}）`;
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = r.status;
    err.data = d;
    throw err;
  }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}

const pad = (n) => String(n).padStart(2, '0');

/** 复制文本到剪贴板。优先 Clipboard API，非安全上下文回退到 execCommand。 */
async function copyText(text, okMsg) {
  if (!text) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      toast(okMsg || '已复制', 'ok');
      return;
    }
    throw new Error('no clipboard api');
  } catch (_) {
    // 回退：隐藏 textarea + execCommand（http://127.0.0.1 之外的场景）
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      toast(ok ? (okMsg || '已复制') : '复制失败，请手动选择文本', ok ? 'ok' : 'err');
    } catch (e2) {
      toast('复制失败：' + e2.message, 'err');
    }
  }
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtSize(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
function fmtNum(v, unit) {
  if (v == null) return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  const s = Number.isInteger(n) ? n.toLocaleString() : n.toFixed(2);
  return unit ? `${s}${unit === '%' ? '%' : ' ' + unit}` : s;
}

/* ---------------------------------------------------------- 视图切换 */
function setView(v) {
  S.view = v;
  const map = {
    workbench: 'viewWorkbench',
    sources: 'viewSources',
    users: 'viewUsers',
    config: 'viewConfig',
    logs: 'viewLogs',
    counters: 'viewCounters',
  };
  for (const [k, id] of Object.entries(map)) {
    const n = $(id);
    if (n) n.style.display = k === v ? '' : 'none';
  }
  document.querySelectorAll('#nav button').forEach(b =>
    b.classList.toggle('on', b.dataset.view === v));
  if (v === 'sources') loadSources();
  if (v === 'users') loadUsers();
  if (v === 'config') loadConfig();
  if (v === 'logs') loadLogs();
  if (v === 'counters') loadCounters();
}

/* ---------------------------------------------------------- 用户与认证（2.1.2） */
async function loadUsers() {
  try {
    S.users = await api('/api/admin/users');
  } catch (e) {
    $('userList').innerHTML = `<div class="hint" style="text-align:left">${esc(e.message)}</div>`;
    return;
  }
  renderUsers();
}

function renderUsers() {
  const box = $('userList');
  box.innerHTML = `<div class="card">
    <div class="card-head">用户列表 <span class="badge">${S.users.length} 个</span></div>
    <div class="card-body" style="padding:8px 10px">
      <table class="metrics" style="font-size:12.3px">
        <thead><tr>
          <th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th>操作</th>
        </tr></thead>
        <tbody>
        ${S.users.map(u => `<tr>
          <td style="font-family:var(--mono)">${esc(u.username)}</td>
          <td>${esc(u.display_name)}</td>
          <td><span class="pill ${u.role === 'admin' ? 'running' : ''}">
            ${u.role === 'admin' ? '管理员' : '分析用户'}</span></td>
          <td><span class="pill ${u.status === 'active' ? 'success' : 'failed'}">
            ${esc(u.status)}</span></td>
          <td style="white-space:nowrap">
            <button class="btn sm" data-uact="pwd" data-uid="${u.id}">重置密码</button>
            <button class="btn sm" data-uact="role" data-uid="${u.id}"
              data-role="${u.role === 'admin' ? 'analyst' : 'admin'}">
              ${u.role === 'admin' ? '降为分析' : '升为管理'}</button>
            <button class="btn sm ${u.status === 'active' ? 'danger' : ''}"
              data-uact="status" data-uid="${u.id}"
              data-status="${u.status === 'active' ? 'disabled' : 'active'}">
              ${u.status === 'active' ? '停用' : '启用'}</button>
          </td>
        </tr>`).join('')}
        </tbody>
      </table>
    </div>
  </div>`;

  box.querySelectorAll('[data-uact]').forEach(btn => {
    btn.addEventListener('click', () => userAction(btn.dataset.uact, btn));
  });
}

async function userAction(act, btn) {
  const uid = Number(btn.dataset.uid);
  try {
    if (act === 'pwd') {
      const pw = prompt('新密码（≥6 位）');
      if (!pw) return;
      const r = await api(`/api/admin/users/${uid}/password`, {
        method: 'POST', body: JSON.stringify({ password: pw }),
      });
      toast(r.message, 'ok');
      return;
    }
    if (act === 'role') {
      const role = btn.dataset.role;
      if (!confirm(`确认把该用户改为 ${role === 'admin' ? '系统管理员' : '分析用户'}？`)) return;
      await api(`/api/admin/users/${uid}/role`, {
        method: 'POST', body: JSON.stringify({ role }),
      });
      toast('角色已更新', 'ok');
      await loadUsers();
      return;
    }
    if (act === 'status') {
      const status = btn.dataset.status;
      if (!confirm(status === 'disabled' ? '停用后该用户无法登录，确认？' : '启用该用户？')) return;
      await api(`/api/admin/users/${uid}/status`, {
        method: 'POST', body: JSON.stringify({ status }),
      });
      toast('状态已更新', 'ok');
      await loadUsers();
    }
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function createUser() {
  const username = $('nuName').value.trim();
  const password = $('nuPwd').value;
  if (!username || !password) { toast('用户名和密码都要填'); return; }
  try {
    await api('/api/admin/users', {
      method: 'POST',
      body: JSON.stringify({
        username, password,
        display_name: $('nuDisplay').value.trim() || username,
        role: $('nuRole').value,
      }),
    });
    toast('用户已创建', 'ok');
    $('addUserCard').style.display = 'none';
    ['nuName', 'nuDisplay', 'nuPwd'].forEach(id => { $(id).value = ''; });
    await loadUsers();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ---------------------------------------------------------- 数据源（2.1.2） */
async function loadSources() {
  try {
    S.sources = await api('/api/admin/data-sources');
  } catch (e) {
    $('sourceList').innerHTML =
      `<div class="hint" style="text-align:left">${esc(e.message)}</div>`;
    return;
  }
  renderSources();
}

function renderSources() {
  const box = $('sourceList');
  if (!S.sources.length) {
    box.innerHTML = `<div class="hint" style="text-align:left; padding:16px">
      还没有登记任何数据源。<br />
      当前使用内置示例库 <code style="font-family:var(--mono)">data/business.db</code>。<br />
      点右上「登记数据源」可以接入你自己的 SQLite 数据库。</div>`;
    return;
  }
  box.innerHTML = S.sources.map(d => {
    const st = d.status === 'ok' ? 'success'
      : d.status === 'error' ? 'failed' : 'queued';
    return `<div class="card" style="margin-bottom:10px">
      <div class="card-head">
        <span>${esc(d.name)}</span>
        <span class="pill ${st}">${esc(d.status)}</span>
        ${d.is_active ? '<span class="pill success">当前使用</span>' : ''}
        <span class="badge">#${d.id}</span>
      </div>
      <div class="card-body" style="padding:10px 12px">
        <div style="font-family:var(--mono); font-size:11.5px; color:var(--ink-2);
                    word-break:break-all">${esc(d.path)}</div>
        <div style="font-size:11.5px; color:var(--ink-3); margin-top:4px">
          ${d.read_only ? '只读' : '可写（不建议）'}
          ${d.last_checked_at ? ' · 最近检查 ' + fmtDate(d.last_checked_at) : ''}
        </div>
        ${d.last_error ? `<div style="color:var(--bad); font-size:11.5px; margin-top:6px">
          ${esc(d.last_error)}</div>` : ''}
        <div style="display:flex; gap:6px; margin-top:10px">
          <button class="btn sm" data-act="inspect" data-id="${d.id}">探查结构</button>
          <button class="btn sm" data-act="test" data-id="${d.id}">测连通</button>
          ${d.is_active ? '' :
            `<button class="btn sm primary" data-act="activate" data-id="${d.id}">设为当前</button>`}
          <button class="btn sm ${d.is_active ? 'danger' : 'danger'}"
            data-act="del" data-id="${d.id}">删除</button>
        </div>
      </div>
    </div>`;
  }).join('');

  box.querySelectorAll('[data-act]').forEach(btn => {
    btn.addEventListener('click', () =>
      sourceAction(btn.dataset.act, Number(btn.dataset.id)));
  });
}

async function sourceAction(act, id) {
  const d = S.sources.find(x => x.id === id) || {};
  try {
    if (act === 'test') {
      const r = await api(`/api/admin/data-sources/${id}/test`, { method: 'POST' });
      toast(r.message, r.status === 'ok' ? 'ok' : 'err');
      await loadSources();
      return;
    }
    if (act === 'inspect') {
      renderInspect(await api(`/api/admin/data-sources/${id}/inspect`));
      return;
    }
    if (act === 'activate') {
      try {
        const r = await api(`/api/admin/data-sources/${id}/activate`,
          { method: 'POST', body: JSON.stringify({}) });
        toast(`${r.message}（完整度 ${r.coverage}%）`, 'ok');
      } catch (e) {
        if (e.status === 400 && /force/.test(e.message)) {
          if (!confirm(e.message.replace(/。如确要切换，请传 force=true$/, '')
            + '\n\n仍要切换吗？切换后大部分分析会判「证据不足」。')) return;
          const r2 = await api(`/api/admin/data-sources/${id}/activate`,
            { method: 'POST', body: JSON.stringify({ force: true }) });
          toast(`${r2.message}（完整度 ${r2.coverage}%）`, 'ok');
        } else {
          throw e;
        }
      }
      await loadSources();
      return;
    }
    if (act === 'del') {
      const isActive = d.is_active;
      const tip = isActive
        ? '这是当前激活的数据源，删除后会自动回落到内置示例库。\n不会删除磁盘上的数据库文件。确认？'
        : '删除这个数据源登记？不会删除磁盘上的数据库文件。';
      if (!confirm(tip)) return;
      const r = await api(`/api/admin/data-sources/${id}`, { method: 'DELETE' });
      toast(r.message || '已删除', 'ok');
      await loadSources();
      return;
    }
  } catch (e) {
    toast(e.message, 'err');
  }
}

function renderInspect(info) {
  const box = $('sourceInspect');
  const miss = info.missing_required || [];
  const opt = info.missing_optional || [];
  const flds = info.field_issues || {};
  const ready = info.ready;

  box.innerHTML = `<div class="card" style="margin-top:10px">
    <div class="card-head">
      结构探查结果
      <span class="pill ${ready ? 'success' : 'cancelled'}">
        ${ready ? '可支撑完整分析' : '结构不完整'}</span>
      <span class="badge">完整度 ${info.coverage}%</span>
    </div>
    <div class="card-body" style="padding:10px 12px">
      <div style="font-size:12px; color:var(--ink-2); margin-bottom:8px">
        共 ${(info.tables || []).length} 张表：${(info.tables || []).map(t =>
          `<code style="font-family:var(--mono); color:var(--accent-2)">${esc(t)}</code>`
        ).join('、') || '（无）'}
      </div>
      ${miss.length ? `<div style="color:var(--bad); font-size:12px; margin-top:8px">
        <b>缺少必需表：</b>${miss.map(esc).join('、')}</div>` : ''}
      ${Object.keys(flds).length ? `<div style="color:var(--warn); font-size:12px; margin-top:8px">
        <b>字段缺失：</b>${Object.entries(flds).map(([t, c]) =>
          `${esc(t)} 缺 ${c.map(esc).join('/')}`).join('；')}</div>` : ''}
      ${opt.length ? `<div style="color:var(--ink-3); font-size:12px; margin-top:8px">
        缺少可选表：${opt.map(esc).join('、')}</div>` : ''}
      ${(info.impact || []).length ? `<div style="margin-top:10px;
        border-top:1px solid var(--line); padding-top:8px">
        <div style="font-size:11.5px; color:var(--ink-3); margin-bottom:4px">对分析的影响</div>
        <ul class="clean" style="margin:0">${info.impact.map(x =>
          `<li style="font-size:12px">${esc(x)}</li>`).join('')}</ul></div>` : ''}
      ${info.error ? `<div style="color:var(--bad); margin-top:8px">${esc(info.error)}</div>` : ''}
    </div>
  </div>`;
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function addSource() {
  const name = $('dsName').value.trim();
  const path = $('dsPath').value.trim();
  const ro = $('dsReadonly').checked;
  if (!name || !path) { toast('名称和路径都要填'); return; }
  try {
    const r = await api('/api/admin/data-sources', {
      method: 'POST', body: JSON.stringify({ name, path, read_only: ro }),
    });
    toast(r.message || '已登记', r.status === 'ok' ? 'ok' : 'err');
    $('addSourceCard').style.display = 'none';
    $('dsName').value = '';
    $('dsPath').value = '';
    await loadSources();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ---------------------------------------------------------- 会话 */
async function loadConvs() {
  try {
    S.convs = await api('/api/chat/ls');
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  renderConvs();
}

function renderConvs() {
  const box = $('convList');
  box.innerHTML = '';
  if (!S.convs.length) {
    box.appendChild(el('div', 'hint', '还没有会话<br />点击「新建」开始'));
    return;
  }
  for (const c of S.convs) {
    const active = c.conversation_id === S.convId;
    const archived = c.status === 'archived';
    const row = el('div', 'conv' + (active ? ' on' : '') + (archived ? ' archived' : ''));
    row.dataset.id = c.conversation_id;
    row.innerHTML = `
      <span class="dot"></span>
      <span class="t" title="${esc(c.title)}">${esc(c.title)}</span>
      ${archived ? '<span class="conv-archived-tag">归档</span>' : ''}
      <span class="acts">
        <button data-act="rename" title="重命名">✎</button>
        <button data-act="archive" title="${archived ? '恢复' : '归档'}">${archived ? '↩' : '⌄'}</button>
        <button data-act="del" class="del" title="删除">✕</button>
      </span>`;
    row.addEventListener('click', (e) => {
      const act = e.target.closest('[data-act]');
      if (act) {
        e.stopPropagation();
        handleConvAction(c, act.dataset.act);
        return;
      }
      selectConv(c.conversation_id);
    });
    box.appendChild(row);
  }
}

async function handleConvAction(conv, act) {
  const id = conv.conversation_id;
  if (act === 'rename') {
    const t = prompt('重命名会话', conv.title);
    if (t == null || !t.trim()) return;
    await api('/api/chat/update', {
      method: 'POST', body: JSON.stringify({ conversation_id: id, title: t.trim() }),
    });
    toast('已重命名', 'ok');
    await loadConvs();
    return;
  }
  if (act === 'archive') {
    const next = conv.status === 'archived' ? 'active' : 'archived';
    await api('/api/chat/update', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: id, status: next }),
    });
    toast(next === 'archived' ? '已归档' : '已恢复', 'ok');
    await loadConvs();
    return;
  }
  if (act === 'del') {
    if (!confirm('删除会话会同时删除消息、附件、任务、结果与文件目录，无法恢复。确认？')) return;
    await api('/api/chat/delete', {
      method: 'POST', body: JSON.stringify({ conversation_ids: [id] }),
    });
    toast('已删除', 'ok');
    if (S.convId === id) {
      S.convId = null; S.lastResult = null; S.lastTaskId = null;
      S.attachments = []; S.trace = [];
      renderChat([]); renderResult(); renderTabs();
    }
    await loadConvs();
  }
}

async function newConv() {
  try {
    const c = await api('/api/chat/create', {
      method: 'POST',
      body: JSON.stringify({ title: '分析 ' + new Date().toLocaleString('zh-CN',
        { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }),
    });
    await loadConvs();
    await selectConv(c.conversation_id);
    $('input').focus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function selectConv(id) {
  S.convId = id;
  S.lastResult = null;
  S.lastTaskId = null;
  S.trace = [];
  renderConvs();
  await Promise.all([loadMessages(), loadAttachments(), loadLastResult()]);
  connectWs();
}

async function loadMessages() {
  if (!S.convId) return renderChat([]);
  try {
    const msgs = await api(`/api/chat/ls/${S.convId}`);
    renderChat(msgs);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function renderChat(msgs) {
  const box = $('chat');
  box.innerHTML = '';
  if (!msgs.length) {
    box.innerHTML = `
      <div class="empty-state">
        <div class="icon">◈</div>
        <h3>从一个模糊的经营问题开始</h3>
        <p>系统会先确认口径，再拆假设取证。</p>
        <div class="samples" id="samples">
          <button data-q="为什么 2024-06 的销售额下滑了？"><span>01</span>为什么 2024-06 的销售额下滑了？</button>
          <button data-q="为什么 2024-06 的退款占比上升了？"><span>02</span>为什么 2024-06 的退款占比上升了？</button>
          <button data-q="销售额"><span>03</span>销售额　（故意缺条件，看它怎么追问）</button>
          <button data-q="为什么 2019-01 的销售额下滑了？"><span>04</span>为什么 2019-01 的销售额下滑了？　（越界区间）</button>
        </div>
      </div>`;
    bindSamples();
    return;
  }
  for (const m of msgs) {
    if (m.role === 'user') {
      const n = el('div', 'msg user', esc(m.content));
      box.appendChild(n);
    } else if (m.role === 'assistant') {
      const isClarify = m.message_type === 'clarify';
      const n = el('div', 'msg bot' + (isClarify ? ' clarify' : ''));
      n.innerHTML = (isClarify ? '<span class="tag">需要补充信息</span><br />' : '') +
        esc(m.content) +
        `<div class="meta">#${m.seq_no} · ${fmtTime(m.created_at)}</div>`;
      box.appendChild(n);
    }
  }
  box.scrollTop = box.scrollHeight;
}

function bindSamples() {
  document.querySelectorAll('#samples button').forEach(b => {
    b.addEventListener('click', () => {
      if (!S.convId) { toast('请先新建或选择一个会话'); return; }
      $('input').value = b.dataset.q;
      $('input').focus();
    });
  });
}

/* ---------------------------------------------------------- 发送 */
async function send() {
  const input = $('input');
  const text = input.value.trim();
  if (!text) return;
  if (!S.convId) { toast('请先新建或选择一个会话'); return; }
  if (S.runningTask) { toast('当前会话已有分析在执行'); return; }

  input.value = '';
  autoGrow(input);
  try {
    const r = await api('/api/chat/send', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: S.convId, text }),
    });
    S.runningTask = r.task_id;
    showRun(r.task_id, 'queued', '排队中');
    await loadMessages();
  } catch (e) {
    input.value = text;
    if (e.status === 409) {
      toast('该会话已有任务在执行，请等待或取消', 'err');
      if (e.data && e.data.running_task_id) {
        S.runningTask = e.data.running_task_id;
        showRun(e.data.running_task_id, 'running', '执行中');
      }
    } else {
      toast(e.message, 'err');
    }
  }
}

function autoGrow(t) {
  t.style.height = 'auto';
  t.style.height = Math.min(148, Math.max(42, t.scrollHeight)) + 'px';
}

/* ---------------------------------------------------------- 任务状态 */
function showRun(taskId, status, step) {
  $('runbar').style.display = '';
  const p = $('runStatus');
  p.textContent = status;
  p.className = 'pill ' + status;
  $('runStep').textContent = step || '';
  const live = status === 'queued' || status === 'running';
  $('btnCancel').disabled = !live;
}

function hideRun() {
  $('runbar').style.display = 'none';
  S.runningTask = null;
}

async function cancelTask() {
  if (!S.runningTask) return;
  try {
    await api(`/api/tasks/${S.runningTask}/cancel`, { method: 'POST' });
    toast('已请求取消');
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ---------------------------------------------------------- WebSocket */
async function connectWs() {
  const gen = ++S.wsGen;
  if (S.ws) { try { S.ws.close(); } catch (_) {} S.ws = null; }
  if (!S.convId) { setConn(false); return; }

  try {
    const t = await api('/api/chat/ws-token', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: S.convId }),
    });
    if (gen !== S.wsGen) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(
      `${proto}://${location.host}/api/chat/ws/chat` +
      `?websocket_token=${encodeURIComponent(t.websocket_token)}` +
      `&conversation_id=${S.convId}`);
    S.ws = ws;

    ws.onopen = () => { if (gen === S.wsGen) setConn(true); };
    ws.onclose = () => {
      if (gen !== S.wsGen) return;
      setConn(false);
      if (S.convId) setTimeout(() => { if (gen === S.wsGen) connectWs(); }, 4000);
    };
    ws.onerror = () => setConn(false);
    ws.onmessage = (ev) => {
      if (gen !== S.wsGen) return;
      let m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      handleEvent(m);
    };
  } catch (e) {
    setConn(false);
  }
}

function setConn(on) {
  const c = $('conn');
  c.classList.toggle('on', on);
  $('connText').textContent = on ? '已连接' : '未连接';
}

async function handleEvent(m) {
  switch (m.type) {
    case 'ping':
      break;

    case 'message_start':
      S.runningTask = m.task_id;
      showRun(m.task_id, 'running', '启动');
      break;

    case 'task_status':
      if (m.replayed) {
        if (m.task_status === 'queued' || m.task_status === 'running') {
          S.runningTask = m.task_id;
          showRun(m.task_id, m.task_status, m.current_step);
        } else if (m.task_status === 'success' || m.task_status === 'failed') {
          hideRun();
          if (m.task_status === 'success' && !S.lastResult) {
            await loadLastResult();
          }
        }
      } else {
        S.runningTask = m.task_id;
        showRun(m.task_id, m.task_status, m.current_step);
      }
      break;

    case 'message_delta':
      break;

    case 'result_ready':
      await loadTaskResult(m.task_id);
      break;

    case 'done':
      hideRun();
      await loadMessages();
      await loadConvs();
      if (m.task_id) await loadTrace(m.task_id);
      break;

    case 'error':
      hideRun();
      toast('分析失败：' + (m.error_message || '未知错误'), 'err');
      await loadMessages();
      break;
  }
}

/* ---------------------------------------------------------- 结果 */
async function loadLastResult() {
  if (!S.convId) { renderResult(); return; }
  try {
    const r = await fetch(`/api/chat/last-result/${S.convId}`);
    // 404 = 该会话还没有分析结果，是正常状态，不是错误
    if (!r.ok) { renderResult(); return; }
    const d = await r.json();
    S.lastResult = d;
    S.lastTaskId = d.task_id;
    renderResult();
    if (d.task_id) await loadTrace(d.task_id);
  } catch (_) {
    renderResult();
  }
}

async function loadTaskResult(taskId) {
  try {
    const d = await api(`/api/results/${taskId}`);
    S.lastResult = d;
    S.lastTaskId = taskId;
    renderResult();
    renderTabs();
  } catch (e) {
    toast(e.message, 'err');
  }
}

function renderTabs() {
  const nEv = (S.lastResult?.evidence_list || []).length;
  $('tabEvN').textContent = nEv;
  $('tabTrN').textContent = S.trace.length;
  $('tabAtN').textContent = S.attachments.length;
  $('btnExport').disabled = !S.lastTaskId;
  const ca = $('btnCopyAll');
  if (ca) ca.disabled = !(S.lastResult && S.lastResult.conclusion_text);
}

/** 拼一份纯文本结果，供「复制全文」使用。 */
function resultAsText() {
  const d = S.lastResult;
  if (!d) return '';
  const L = [];
  L.push('【问题定义】', d.problem_definition || '（无）', '');

  if ((d.key_metrics || []).length) {
    L.push('【关键指标】');
    for (const m of d.key_metrics) {
      const dl = m.delta_pct == null ? '—' : (m.delta_pct > 0 ? '+' : '') + m.delta_pct + '%';
      L.push(`· ${m.metric_name}：${m.metric_value}${m.metric_unit}`
        + `（基线 ${m.baseline_value ?? '—'}，变动 ${dl}）`);
      if (m.definition) L.push(`  口径：${m.definition}`);
    }
    L.push('');
  }

  if ((d.hypotheses || []).length) {
    L.push('【假设验证】');
    for (const h of d.hypotheses) {
      L.push(`· [${h.verdict}] ${h.statement}`);
      L.push(`  条件：${h.criterion}`);
      L.push(`  依据：${h.reason}`);
    }
    L.push('');
  }

  if ((d.evidence_list || []).length) {
    L.push('【证据列表】');
    d.evidence_list.forEach((e, i) => {
      L.push(`${i + 1}. ${e.source_name}（${e.source_type}，置信度 ${e.confidence}）`);
      L.push(`   ${e.evidence_text}`);
    });
    L.push('');
  }

  L.push('【归因结论】', d.conclusion_text || '（无）', '');
  const miss = (d.missing_data_text || '').trim();
  L.push('【待补充数据】', miss || '无缺口', '');
  const acts = (d.next_action_text || '').trim();
  L.push('【下一步建议】', acts || '（无）');
  return L.join('\n');
}

function renderResult() {
  const pane = $('resultPane');
  pane.innerHTML = '';
  renderTabs();

  if (S.tab === 'evidence') return renderEvidencePane(pane);
  if (S.tab === 'trace') return renderTracePane(pane);
  if (S.tab === 'files') return renderFilesPane(pane);

  const d = S.lastResult;
  if (!d) {
    pane.innerHTML = '<div class="hint">尚无分析结果<br />完成一次分析后会在这里展示</div>';
    return;
  }

  // 澄清轮
  if (d.needs_clarification || !(d.hypotheses || []).length && d.clarify_question) {
    const c = el('div', 'card');
    c.innerHTML = `
      <div class="card-head">需要补充信息</div>
      <div class="card-body" style="color:var(--warn)">
        ${esc(d.clarify_question || d.conclusion_text)}
      </div>`;
    pane.appendChild(c);
  }

  // 问题定义
  if (d.problem_definition) {
    const c = el('div', 'card');
    c.innerHTML = `
      <div class="card-head">问题定义</div>
      <div class="card-body" style="color:var(--ink-1)">${esc(d.problem_definition)}</div>`;
    pane.appendChild(c);
  }

  // 关键指标
  if ((d.key_metrics || []).length) {
    const c = el('div', 'card');
    const rows = d.key_metrics.map(m => {
      const dl = m.delta_pct;
      const cls = dl == null ? '' : (dl > 0 ? 'up' : 'down');
      const arrow = dl == null ? '' : (dl > 0 ? '↑' : '↓');
      return `<tr>
        <td>${esc(m.metric_name)}</td>
        <td class="num">${fmtNum(m.metric_value, m.metric_unit)}</td>
        <td class="num" style="color:var(--ink-3)">${m.baseline_value != null ? fmtNum(m.baseline_value) : '—'}</td>
        <td class="num ${cls}">${dl != null ? (dl > 0 ? '+' : '') + dl + '% ' + arrow : '—'}</td>
      </tr>
      <tr><td colspan="4" class="def"><em>口径</em>　${esc(m.definition || '—')}</td></tr>`;
    }).join('');
    c.innerHTML = `
      <div class="card-head">关键指标
        <span class="badge">${d.key_metrics.length} 项</span></div>
      <div class="card-body" style="padding:8px 10px">
        <table class="metrics">
          <thead><tr>
            <th>指标</th><th style="text-align:right">本期</th>
            <th style="text-align:right">基线</th><th style="text-align:right">变动</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
    pane.appendChild(c);
  }

  // 假设验证
  if ((d.hypotheses || []).length) {
    const c = el('div', 'card');
    const vd = { '成立': 'ok', '不成立': 'no', '证据不足': 'unk' };
    const mk = { '成立': '✓', '不成立': '✗', '证据不足': '?' };
    const items = d.hypotheses.map((h, i) => {
      const k = vd[h.verdict] || 'unk';
      const evis = (h.evidence || []).map(e =>
        `<div class="evi">${esc(e.evidence_text)}</div>`).join('');
      return `<div class="hyp ${k}${h.verdict === '成立' ? ' open' : ''}" data-i="${i}">
        <div class="hyp-head">
          <span class="mark">${mk[h.verdict] || '·'}</span>
          <span class="txt">
            <div class="stmt">${esc(h.statement)}</div>
          </span>
          <span class="vd">${esc(h.verdict)}</span>
        </div>
        <div class="hyp-detail">
          <div class="kv"><b>判定条件</b>${esc(h.criterion)}</div>
          <div class="kv"><b>判定依据</b>${esc(h.reason)}</div>
          ${evis}
        </div>
      </div>`;
    }).join('');
    c.innerHTML = `
      <div class="card-head">假设验证
        <span class="badge">${d.hypotheses.length} 个</span></div>
      <div class="card-body" style="padding:9px 10px">${items}</div>`;
    pane.appendChild(c);
    c.querySelectorAll('.hyp-head').forEach(h => {
      h.addEventListener('click', () => h.parentElement.classList.toggle('open'));
    });
  }

  // 结论
  if (d.conclusion_text) {
    const c = el('div', 'card');
    c.innerHTML = `
      <div class="card-head">归因结论
        <span class="badge"></span>
        <button class="btn sm" data-copy="conclusion" style="margin-left:auto">复制</button>
      </div>
      <div class="card-body">
        <div class="conclusion" id="conclText">${esc(d.conclusion_text)}</div>
      </div>`;
    c.querySelector('[data-copy=conclusion]').addEventListener('click', () =>
      copyText(d.conclusion_text, '结论已复制'));
    pane.appendChild(c);
  }

  // 待补充数据
  const miss = (d.missing_data_text || '').trim();
  {
    const c = el('div', 'card');
    const body = miss
      ? `<ul class="clean">${miss.split('\n').filter(Boolean)
          .map(l => `<li>${esc(l.replace(/^-\s*/, ''))}</li>`).join('')}</ul>`
      : '<span style="color:var(--ok)">本次无数据缺口</span>';
    c.innerHTML = `
      <div class="card-head">待补充数据</div>
      <div class="card-body">${body}</div>`;
    pane.appendChild(c);
  }

  // 下一步建议
  const acts = (d.next_action_text || '').trim();
  if (acts) {
    const c = el('div', 'card');
    c.innerHTML = `
      <div class="card-head">下一步建议</div>
      <div class="card-body"><ol class="clean">${acts.split('\n').filter(Boolean)
        .map(l => `<li>${esc(l.replace(/^-\s*/, ''))}</li>`).join('')}</ol></div>`;
    pane.appendChild(c);
  }

  // 说明
  if (d.degraded_note) {
    const c = el('div', 'card');
    c.innerHTML = `
      <div class="card-head">说明</div>
      <div class="card-body" style="color:var(--warn)">${esc(d.degraded_note)}</div>`;
    pane.appendChild(c);
  }
}

function renderEvidencePane(pane) {
  const evs = S.lastResult?.evidence_list || [];
  if (!evs.length) {
    pane.innerHTML = '<div class="hint">本次分析无证据记录</div>';
    return;
  }
  const typeMap = {
    database: 'DB', attachment: '附件', file: '文件',
    computation: '计算',
  };
  const confMap = { '高': 'high', '中': 'mid', '低': 'low' };
  const c = el('div', 'card');
  c.innerHTML = `
    <div class="card-head">证据列表
      <span class="badge">${evs.length} 条</span>
      <button class="btn sm" data-copy="evidence" style="margin-left:auto">复制</button>
    </div>
    <div class="card-body" style="padding:6px 12px">
      ${evs.map((e, i) => `
        <div class="evi-item">
          <span class="idx">${i + 1}</span>
          <div class="body">
            <div class="src">
              <span>${esc(e.source_name)}</span>
              <span class="type">${esc(typeMap[e.source_type] || e.source_type)}</span>
              <span class="conf ${confMap[e.confidence] || 'mid'}">${esc(e.confidence)}</span>
            </div>
            <div class="txt">${esc(e.evidence_text)}</div>
          </div>
        </div>`).join('')}
    </div>`;
  c.querySelector('[data-copy=evidence]').addEventListener('click', () => {
    const txt = evs.map((e, i) =>
      `${i + 1}. ${e.source_name}（${e.source_type}，置信度 ${e.confidence}）\n   ${e.evidence_text}`
    ).join('\n');
    copyText(txt, '证据已复制');
  });
  pane.appendChild(c);
}

async function loadTrace(taskId) {
  if (!taskId) return;
  try {
    S.trace = await api(`/api/tasks/${taskId}/tools`);
  } catch (_) {
    S.trace = [];
  }
  renderTabs();
  if (S.tab === 'trace') renderResult();
}

function renderTracePane(pane) {
  if (!S.trace.length) {
    pane.innerHTML = '<div class="hint">本次分析未调用工具<br />（澄清轮次通常不取数）</div>';
    return;
  }
  const cls = (st) => st === 'success' ? 'success'
    : st === 'rejected' ? 'cancelled' : 'failed';

  // 把调用参数翻译成人能看懂的一行说明
  const describe = (t) => {
    const a = t.arguments || {};
    if (a.label) {
      return `${a.label} · ${a.period || ''}`;
    }
    if (a.keyword) return `检索「${a.keyword}」`;
    if (a.path) return `文件 ${a.path}`;
    if (a.purpose) return a.purpose;
    if (a.metric) return a.metric;
    return t.result_summary || t.error_message || '';
  };

  const c = el('div', 'card');
  c.innerHTML = `
    <div class="card-head">工具调用轨迹
      <span class="badge">${S.trace.length} 次</span></div>
    <div class="card-body trace" style="padding:6px 12px">
      ${S.trace.map(t => `
        <div class="row">
          <span class="nm">${esc(t.tool_name)}</span>
          <span class="pill ${cls(t.status)}">${esc(t.status)}</span>
          <span class="sum" title="${esc(t.result_summary || t.error_message || '')}">
            ${esc(describe(t))}</span>
          <span class="ms">${t.duration_ms}ms</span>
        </div>`).join('')}
    </div>`;
  pane.appendChild(c);
}

function renderFilesPane(pane) {
  const wrap = el('div');
  wrap.innerHTML = `
    <div class="card">
      <div class="card-head">
        当前会话附件
        <span class="spacer" style="margin-left:auto"></span>
        <label class="btn sm primary" style="cursor:pointer">
          上传<input type="file" id="fileInput2" style="display:none" />
        </label>
      </div>
      <div class="card-body" style="padding:9px 10px" id="attBox"></div>
    </div>`;
  pane.appendChild(wrap);
  renderAttachments($('attBox'));
  const fi = $('fileInput2');
  if (fi) {
    fi.addEventListener('change', async (e) => {
      const f = e.target.files[0];
      if (f) await uploadFile(f);
      e.target.value = '';
    });
  }
}

function renderAttachments(box) {
  if (!box) return;
  if (!S.attachments.length) {
    box.innerHTML = '<div class="hint" style="padding:14px">暂无附件<br />'
      + '<span style="font-size:11.5px">支持 txt / md / csv / json 文本解析并参与证据检索</span></div>';
    return;
  }
  box.innerHTML = S.attachments.map(a => {
    const ok = a.parse_status === 'success';
    const bad = a.parse_status === 'failed';
    const cls = ok ? 'ok' : bad ? 'bad' : 'pending';
    const ext = (a.file_type || 'file').slice(0, 4);
    return `<div class="att">
      <span class="ico">${esc(ext)}</span>
      <div class="info">
        <div class="nm" title="${esc(a.file_name)}">${esc(a.file_name)}</div>
        <div class="sub ${cls}">
          ${esc(a.parse_status)} · ${fmtSize(a.file_size)} · ${fmtDate(a.created_at)}
        </div>
      </div>
      <div class="acts">
        <button class="btn sm" data-open="${a.attachment_id}">下载</button>
        <button class="btn sm danger" data-rm="${a.attachment_id}">删除</button>
      </div>
    </div>`;
  }).join('');
  box.querySelectorAll('[data-open]').forEach(b =>
    b.addEventListener('click', () =>
      window.open(`/api/attachment/get?attachment_id=${b.dataset.open}`, '_blank')));
  box.querySelectorAll('[data-rm]').forEach(b =>
    b.addEventListener('click', async () => {
      await api('/api/attachment/delete', {
        method: 'POST',
        body: JSON.stringify({ attachment_id: Number(b.dataset.rm) }),
      });
      toast('附件已删除', 'ok');
      await loadAttachments();
    }));
}

async function loadAttachments() {
  if (!S.convId) { S.attachments = []; renderTabs(); return; }
  try {
    S.attachments = await api(`/api/attachments/${S.convId}`);
  } catch (_) {
    S.attachments = [];
  }
  renderTabs();
  if (S.tab === 'files') renderResult();
}

async function uploadFile(file) {
  if (!S.convId) { toast('请先选择会话'); return; }
  const fd = new FormData();
  fd.append('conversation_id', S.convId);
  fd.append('file', file);
  try {
    const r = await fetch('/api/attachment/upload', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) { toast(d.detail || '上传失败', 'err'); return; }
    toast(d.parse_status === 'success' ? '上传并解析成功' : `已上传，解析状态：${d.parse_status}`,
      d.parse_status === 'success' ? 'ok' : 'err');
    await loadAttachments();
  } catch (e) {
    toast('上传失败：' + e.message, 'err');
  }
}

/* ---------------------------------------------------------- 配置页 */
async function loadConfig() {
  try {
    S.config = await api('/api/admin/config');
  } catch (e) {
    $('configGrid').innerHTML = `<div class="hint">${esc(e.message)}</div>`;
    return;
  }
  const grid = $('configGrid');
  grid.innerHTML = '';
  const desc = {
    delta_threshold_pct: '显著变化阈值（%）',
    min_rows_for_high_confidence: '高置信度所需样本量',
    db_query_max_rows: '单次查询最大返回行数',
    file_read_max_bytes: '单文件读取上限',
    command_timeout_sec: '命令超时（秒）',
    command_output_max_chars: '命令输出上限（字符）',
    task_max_steps: '单任务最大步骤数',
    task_max_seconds: '单任务最长耗时（秒）',
    summary_trigger_messages: '摘要触发消息数',
    summary_keep_recent: '摘要保留最近条数',
    generation_source: '生成来源',
    llm_base_url: 'LLM 地址',
    llm_model: 'LLM 模型',
    llm_api_key: 'LLM 密钥',
    enable_attachment: '附件上传',
    enable_command_tool: '命令执行工具',
    enable_file_tools: '文件读写工具',
    enable_text_search: '附件文本检索',
    enable_context_summary: '上下文摘要',
    enable_export: '结果导出',
  };
  const effect = {
    enable_attachment: '关闭后无法上传附件',
    enable_command_tool: '关闭后分析不能执行命令',
    enable_file_tools: '关闭后不能读写中间文件',
    enable_text_search: '关闭后不从附件检索证据',
    enable_context_summary: '关闭后不生成会话摘要',
    enable_export: '关闭后不可导出结果文件',
  };
  const editable = new Set([
    'delta_threshold_pct', 'db_query_max_rows', 'command_timeout_sec',
    'task_max_steps', 'summary_trigger_messages', 'summary_keep_recent',
    'enable_attachment', 'enable_command_tool', 'enable_file_tools',
    'enable_text_search', 'enable_context_summary', 'enable_export',
  ]);

  // 先渲染开关区，再渲染数值区
  const switches = Object.entries(S.config).filter(([k]) => k.startsWith('enable_'));
  const numbers = Object.entries(S.config).filter(([k]) => !k.startsWith('enable_'));

  const sw = el('div', 'card');
  sw.style.gridColumn = '1 / -1';
  sw.innerHTML = `<div class="card-head">功能开关
    <span class="badge">改完点「重载配置」生效</span></div>
    <div class="card-body" style="padding:10px 12px">
      ${switches.map(([k, v]) => `
        <div style="display:flex; align-items:center; gap:12px; padding:6px 0;
                    border-bottom:1px solid var(--bg-3)">
          <div style="flex:1">
            <div style="font-family:var(--mono); font-size:12px">${esc(k)}</div>
            <div style="font-size:11px; color:var(--ink-3)">
              ${esc(desc[k] || '')}${effect[k] ? ' · ' + esc(effect[k]) : ''}</div>
          </div>
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer">
            <input type="checkbox" data-sw="${esc(k)}" ${v ? 'checked' : ''} />
            <span style="font-size:11.5px; color:${v ? 'var(--ok)' : 'var(--ink-3)'}">
              ${v ? '开启' : '关闭'}</span>
          </label>
        </div>`).join('')}
    </div>`;
  grid.appendChild(sw);

  for (const [k, v] of numbers) {
    const item = el('div', 'cfg-item');
    const isNum = typeof v === 'number';
    item.innerHTML = `
      <div class="k">
        <b>${esc(k)}</b>
        <span>${esc(desc[k] || '')}</span>
      </div>
      ${editable.has(k)
        ? `<input data-k="${esc(k)}" value="${esc(v)}" ${isNum ? 'type="number" step="any"' : ''} />`
        : `<span style="font-family:var(--mono);font-size:11.5px;color:var(--ink-3);
             max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
             title="${esc(v)}">${esc(String(v) || '（空）')}</span>`}`;
    grid.appendChild(item);
  }
}

async function reloadConfig() {
  const updates = {};
  document.querySelectorAll('#configGrid input[data-k]').forEach(inp => {
    const k = inp.dataset.k;
    const raw = inp.value.trim();
    const orig = S.config[k];
    const val = typeof orig === 'number' ? Number(raw) : raw;
    if (val !== orig && !(typeof orig === 'number' && Number.isNaN(val))) {
      updates[k] = val;
    }
  });
  // 功能开关
  document.querySelectorAll('#configGrid input[data-sw]').forEach(inp => {
    const k = inp.dataset.sw;
    const orig = !!S.config[k];
    const val = inp.checked;
    if (val !== orig) updates[k] = val;
  });
  try {
    const r = await api('/api/admin/reload', {
      method: 'POST', body: JSON.stringify({ updates }),
    });
    const n = Object.keys(r.changed || {}).length;
    $('cfgChanged').textContent = n ? `已变更 ${n} 项` : '无变更';
    if (n) {
      const lines = Object.entries(r.changed)
        .map(([k, v]) => `${k}: ${v.before} → ${v.after}`).join('；');
      toast(lines, 'ok');
    } else {
      toast('配置已重载，未检测到变更', 'ok');
    }
    await loadConfig();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ---------------------------------------------------------- 日志页 */
async function loadLogs() {
  const lv = $('logLevel').value;
  try {
    const url = '/api/admin/logs?limit=300' + (lv ? `&level=${lv}` : '');
    S.logs = await api(url);
  } catch (e) {
    $('logBody').innerHTML =
      `<tr><td colspan="5" style="color:var(--bad)">${esc(e.message)}</td></tr>`;
    return;
  }
  const body = $('logBody');
  if (!S.logs.length) {
    body.innerHTML = '<tr><td colspan="5" class="hint">暂无日志</td></tr>';
    return;
  }
  body.innerHTML = S.logs.map(r => `
    <tr class="${esc(r.log_level)}">
      <td class="ts">${esc(r.created_at.replace('T', ' ').slice(0, 19))}</td>
      <td class="lv">${esc(r.log_level)}</td>
      <td style="color:var(--ink-3)">${esc(r.log_type)}</td>
      <td style="color:var(--accent-2)">${r.task_id ?? '—'}</td>
      <td>${esc(r.log_content)}</td>
    </tr>`).join('');
}

/* ---------------------------------------------------------- 计数器页 */
async function loadCounters() {
  try {
    S.counters = await api('/api/admin/rejections');
  } catch (e) {
    $('counterGrid').innerHTML = `<div class="hint">${esc(e.message)}</div>`;
    return;
  }
  const grid = $('counterGrid');
  if (!S.counters.length) {
    grid.innerHTML = '<div class="hint">暂无拒绝记录<br />'
      + '<span style="font-size:11.5px">说明所有拒绝路径都还没被触发过</span></div>';
    return;
  }
  grid.innerHTML = S.counters.map(r => `
    <div class="counter ${r.count === 0 ? 'zero' : ''}">
      <div class="code">${esc(r.code)}</div>
      <div class="n">${r.count}</div>
    </div>`).join('');
}

/* ---------------------------------------------------------- 事件绑定 */
function bind() {
  $('userAv').textContent = (ME.name || '?').slice(0, 1);

  document.querySelectorAll('#nav button').forEach(b =>
    b.addEventListener('click', () => setView(b.dataset.view)));

  $('btnNewConv').addEventListener('click', newConv);
  $('btnSend').addEventListener('click', send);
  $('btnCancel').addEventListener('click', cancelTask);

  const input = $('input');
  input.addEventListener('input', () => autoGrow(input));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      send();
    }
  });

  document.querySelectorAll('#resultTabs button').forEach(b =>
    b.addEventListener('click', () => {
      S.tab = b.dataset.tab;
      document.querySelectorAll('#resultTabs button').forEach(x =>
        x.classList.toggle('on', x === b));
      renderResult();
    }));

  $('btnExport').addEventListener('click', () => {
    if (!S.lastTaskId) return;
    window.open(`/api/results/${S.lastTaskId}/download`, '_blank');
  });

  const cpAll = $('btnCopyAll');
  if (cpAll) cpAll.addEventListener('click', () =>
    copyText(resultAsText(), '结果全文已复制'));

  const rc = $('btnReloadCfg');
  if (rc) rc.addEventListener('click', reloadConfig);
  const rl = $('btnReloadLogs');
  if (rl) rl.addEventListener('click', loadLogs);
  const rcn = $('btnReloadCounters');
  if (rcn) rcn.addEventListener('click', loadCounters);
  const ll = $('logLevel');
  if (ll) ll.addEventListener('change', loadLogs);

  // 数据源
  const radd = $('btnAddSource');
  if (radd) radd.addEventListener('click', () => {
    const c = $('addSourceCard');
    c.style.display = c.style.display === 'none' ? '' : 'none';
    if (c.style.display === '') $('dsName').focus();
  });
  const rcan = $('btnCancelAdd');
  if (rcan) rcan.addEventListener('click', () => {
    $('addSourceCard').style.display = 'none';
  });
  const rsave = $('btnSaveSource');
  if (rsave) rsave.addEventListener('click', addSource);
  const rsrc = $('btnReloadSources');
  if (rsrc) rsrc.addEventListener('click', loadSources);

  // 用户
  const uadd = $('btnAddUser');
  if (uadd) uadd.addEventListener('click', () => {
    const c = $('addUserCard');
    c.style.display = c.style.display === 'none' ? '' : 'none';
    if (c.style.display === '') $('nuName').focus();
  });
  const ucan = $('btnCancelUser');
  if (ucan) ucan.addEventListener('click', () => {
    $('addUserCard').style.display = 'none';
  });
  const usave = $('btnSaveUser');
  if (usave) usave.addEventListener('click', createUser);
  const urel = $('btnReloadUsers');
  if (urel) urel.addEventListener('click', loadUsers);
}

/* ---------------------------------------------------------- 启动 */
(async function boot() {
  bind();
  bindSamples();
  await loadConvs();
  if (S.convs.length) {
    await selectConv(S.convs.find(c => c.status !== 'archived')?.conversation_id
      || S.convs[0].conversation_id);
  }
  // 管理员额外入口
  if (ME.role === 'admin') {
    const nav = $('nav');
    if (nav && !nav.querySelector('[data-view="counters"]')) {
      const b = el('button', '', '拒绝计数');
      b.dataset.view = 'counters';
      b.addEventListener('click', () => setView('counters'));
      nav.appendChild(b);
    }
  }
})();
