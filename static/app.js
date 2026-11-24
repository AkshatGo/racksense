/* RackSense dashboard — vanilla ES2020, no build step. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  token: localStorage.getItem('rs_token') || '',
  user: JSON.parse(localStorage.getItem('rs_user') || 'null'),
  snap: null,
  view: 'dashboard',
  range: 240,
  ws: null,
  retry: 0,
  seenAlerts: new Set(),
  firstPaint: true,
  menuOpen: null,
  charts: [],
};

/* ------------------------------------------------------------------ utils -- */
const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const kg = (v, digits = 2) => Number(v || 0).toFixed(digits);

function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 10) return 'just now';
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function hhmm(ts) {
  return new Date(ts * 1000).toLocaleTimeString('en-GB', {
    timeZone: state.tz || 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

function toast(msg, bad = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, Object.assign({}, options, { headers }));
  if (res.status === 401) {
    signOut();
    throw new Error('Session expired — sign in again.');
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
}

/* ------------------------------------------------------------------- auth -- */
async function signIn(username, password) {
  const out = await api('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
  state.token = out.token;
  state.user = out.user;
  localStorage.setItem('rs_token', out.token);
  localStorage.setItem('rs_user', JSON.stringify(out.user));
  start();
}

function signOut() {
  const token = state.token;
  state.token = '';
  state.user = null;
  localStorage.removeItem('rs_token');
  localStorage.removeItem('rs_user');
  if (token) fetch('/api/auth/logout', { method: 'POST', headers: { Authorization: `Bearer ${token}` } }).catch(() => {});
  if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
}

$('#loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#loginBtn');
  $('#loginError').textContent = '';
  btn.disabled = true;
  btn.textContent = 'Signing in…';
  try {
    await signIn($('#username').value.trim(), $('#password').value);
  } catch (err) {
    $('#loginError').textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in';
  }
});

$('#logoutBtn').addEventListener('click', signOut);

$('#demoBtn').addEventListener('click', async () => {
  const btn = $('#demoBtn');
  btn.disabled = true;
  try {
    const out = await api('/api/auth/demo', { method: 'POST' });
    state.token = out.token;
    state.user = out.user;
    localStorage.setItem('rs_token', out.token);
    localStorage.setItem('rs_user', JSON.stringify(out.user));
    start();
  } catch (e) {
    $('#loginError').textContent = e.message;
  } finally { btn.disabled = false; }
});

/* ------------------------------------------------------------------ shell -- */
async function start() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  const u = state.user;
  if (u) {
    $('#userName').textContent = u.name;
    $('#userRole').textContent = u.role;
    $('#avatar').textContent = (u.name || '?').trim().charAt(0).toUpperCase();
  }
  await refresh();
  connect();
  loadChart();
}

const VIEW_META = {
  dashboard: ['Overview', "Today's floor"],
  log: ['Records', 'Inventory log'],
  devices: ['Hardware', 'Devices'],
  alerts: ['Safety', 'Alerts'],
};

function setView(view) {
  state.view = view;
  $$('.view').forEach((v) => v.classList.add('hidden'));
  $(`#view-${view}`).classList.remove('hidden');
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  const [crumb, title] = VIEW_META[view];
  $('#crumb').textContent = crumb;
  $('#pageTitle').textContent = title;
  if (view === 'log') loadLog();
  if (view === 'alerts') loadAlertHistory();
}

$$('.nav-item').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));

/* ------------------------------------------------------------- live feed -- */
function connect() {
  if (state.ws) { try { state.ws.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}`);
  state.ws = ws;
  ws.onopen = () => {
    state.retry = 0;
    $('#livePill').classList.remove('off');
    $('#liveText').textContent = 'Live';
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'snapshot') render(msg);
  };
  ws.onclose = () => {
    $('#livePill').classList.add('off');
    $('#liveText').textContent = 'Reconnecting';
    const delay = Math.min(8000, 800 * 2 ** state.retry++);
    setTimeout(() => { if (state.token) connect(); }, delay);
  };
}

setInterval(() => {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send('ping');
}, 25000);

async function refresh() {
  try {
    const snap = await api('/api/snapshot');
    render(snap);
  } catch (e) {
    /* snapshot endpoint is the WS payload; fall back to pieces */
    try {
      const [racks, overview, events, alerts] = await Promise.all([
        api('/api/racks'), api('/api/overview'),
        api('/api/events?limit=14'), api('/api/alerts?state=open'),
      ]);
      render({ racks: racks.racks, overview, events: events.events, alerts: alerts.alerts, sim: { enabled: true } });
    } catch (err) {
      toast(err.message, true);
    }
  }
}

/* --------------------------------------------------------------- render -- */
function render(snap) {
  state.snap = snap;
  if (snap.tz) state.tz = snap.tz;
  if (snap.sim) {
    $('#simSwitch').setAttribute('aria-checked', String(!!snap.sim.enabled));
  }
  renderKpis(snap.overview);
  renderAlerts(snap.alerts);
  if (!state.menuOpen) renderRacks(snap.racks);
  renderTape(snap.events);
  renderDevices(snap.racks);
  if (state.view === 'log') scheduleLogReload();
  if (state.view === 'alerts') loadAlertHistory();
  state.firstPaint = false;
}

function renderKpis(o) {
  if (!o) return;
  const openAlerts = o.open_alerts || 0;
  $('#kpis').innerHTML = `
    <div class="kpi hero">
      <div class="label">Removed today</div>
      <div class="value">${kg(o.removed_kg_today, 1)}<small>kg</small></div>
      <div class="foot">${o.items_removed_today} items estimated · ${o.alerts_today} theft alert${o.alerts_today === 1 ? '' : 's'}</div>
    </div>
    <div class="kpi ${openAlerts ? 'alerting' : ''}">
      <div class="label">Open alerts</div>
      <div class="value">${openAlerts}</div>
      <div class="foot">${o.racks_armed} rack${o.racks_armed === 1 ? '' : 's'} armed</div>
    </div>
    <div class="kpi">
      <div class="label">Load on shelves</div>
      <div class="value">${kg(o.total_weight_kg, 1)}<small>kg</small></div>
      <div class="foot">of ${kg(o.capacity_kg, 0)} kg rated capacity</div>
    </div>
    <div class="kpi">
      <div class="label">Racks online</div>
      <div class="value">${o.racks_online}<small>/ ${o.racks_total}</small></div>
      <div class="foot">${o.low_stock_racks} low on stock</div>
    </div>`;

  const count = $('#alertCount');
  count.textContent = openAlerts;
  count.classList.toggle('hidden', openAlerts === 0);
  $('#alertSub').textContent = openAlerts ? `${openAlerts} waiting` : 'nothing waiting';
}

function renderAlerts(alerts) {
  const list = $('#alertList');
  const open = (alerts || []).filter((a) => a.state !== 'resolved');
  if (!open.length) {
    list.innerHTML = '<div class="empty-note">No open alerts. Armed racks will report here the moment stock drops.</div>';
    return;
  }
  list.innerHTML = open.map((a) => {
    const isNew = !state.seenAlerts.has(a.id);
    state.seenAlerts.add(a.id);
    return `
      <div class="alert-item ${a.state === 'acknowledged' ? 'ack' : ''} ${isNew ? 'new' : ''}" style="--i:${a.id}">
        <div class="top"><span class="pulse"></span><span>${esc(a.rack_code)}</span><span>·</span><span>${esc(a.time)}</span>
          <span style="margin-left:auto">${a.state === 'acknowledged' ? 'Acknowledged' : 'Open'}</span></div>
        <div class="msg">${esc(a.message)}</div>
        <div class="actions">
          ${a.state === 'open' ? `<button class="btn btn-sm" data-action="ack" data-id="${a.id}">Acknowledge</button>` : ''}
          <button class="btn btn-sm btn-danger" data-action="resolve" data-id="${a.id}">Resolve</button>
        </div>
      </div>`;
  }).join('');
}

function renderRacks(racks) {
  if (!racks) return;
  $('#rackCount').textContent = `${racks.length} shelves`;
  const html = racks.map((r, i) => {
    const pct = Math.max(0, Math.min(100, r.stock_pct));
    const fillClass = pct <= 5 ? 'critical' : r.low_stock ? 'low' : '';
    const tarePct = Math.max(0, Math.min(100, (r.tare_kg / (r.capacity_kg || 1)) * 100));
    const maxCell = Math.max(1, ...r.cells_g);
    const cells = (r.cells_g.length ? r.cells_g : [0, 0, 0, 0]).map((g) => {
      const size = g > 0 ? 3 + Math.round((g / maxCell) * 6) : 0;
      return `<span class="cell ${g <= 0 ? 'zero' : ''}" title="${kg(g / 1000, 2)} kg"><i style="width:${size}px;height:${size}px"></i></span>`;
    }).join('');
    const lastEvent = (state.snap?.events || []).find((e) => e.rack_id === r.id);
    return `
      <article class="rack ${r.open_alerts ? 'alerted' : ''} ${r.online ? '' : 'offline'} reveal" style="--i:${i}">
        <div class="rack-top">
          <span class="status-dot ${r.online ? '' : 'off'}"></span>
          <div>
            <div class="rack-code">${esc(r.code)}</div>
            <div class="rack-name">${esc(r.name)}${r.location ? ' · ' + esc(r.location) : ''}${r.tier ? ' · ' + esc(r.tier) : ''}</div>
          </div>
          <div class="spacer"></div>
          ${r.armed ? '<span class="chip armed">Armed</span>' : ''}
          ${r.low_stock ? '<span class="chip low">Low</span>' : ''}
          <div class="dropdown">
            <button class="menu-btn" data-menu="${r.id}" aria-label="Rack actions">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>
            </button>
            <div class="dropdown-menu" id="menu-${r.id}">
              <button data-action="sim" data-id="${r.id}" data-kind="sale">Simulate a sale</button>
              <button data-action="sim" data-id="${r.id}" data-kind="theft">Simulate a theft</button>
              <button data-action="sim" data-id="${r.id}" data-kind="restock">Restock to capacity</button>
              <button data-action="sim" data-id="${r.id}" data-kind="bump">Nudge the shelf</button>
              <div class="sep"></div>
              <button data-action="tare" data-id="${r.id}">Tare now</button>
              <button data-action="edit" data-id="${r.id}">Edit rack</button>
              ${state.user?.role === 'owner' ? `<button class="danger" data-action="delete" data-id="${r.id}">Remove rack</button>` : ''}
            </div>
          </div>
        </div>

        <div class="rack-weight">
          <span class="kg">${kg(r.weight_kg, 2)}</span>
          <span class="of">kg of ${kg(r.capacity_kg, 0)} kg</span>
          <span class="pct">${Math.round(r.stock_pct)}%</span>
        </div>

        <div class="scale">
          <div class="scale-track">
            <div class="scale-fill ${fillClass}" style="width:${pct}%"></div>
            <div class="scale-tare" style="left:${tarePct}%" title="tare ${kg(r.tare_kg, 2)} kg"></div>
          </div>
          <div class="scale-legend"><span>0</span><span>tare ${kg(r.tare_kg, 1)} kg</span><span>${kg(r.capacity_kg, 0)} kg</span></div>
        </div>

        <div class="cell-compass">
          <div class="cells">${cells}</div>
          <div class="cell-meta">
            four corner cells, live share<br>
            <b>${r.cells_g.map((g) => kg(g / 1000, 1)).join(' / ')}</b> kg
          </div>
        </div>

        <div class="rack-foot">
          <span class="last">${lastEvent ? esc(lastEvent.status) + ' · ' + ago(lastEvent.ts) : 'no movement yet'}</span>
          <div class="spacer"></div>
          <button class="switch" data-action="arm" data-id="${r.id}" data-armed="${r.armed ? 1 : 0}"
            role="switch" aria-checked="${r.armed ? 'true' : 'false'}"
            aria-label="Anti-theft arming for ${esc(r.code)}" title="${r.armed ? 'Disarm' : 'Arm'} anti-theft"></button>
        </div>
      </article>`;
  }).join('');
  $('#rackGrid').innerHTML = html;

  const sel = $('#logRack');
  const current = sel.value;
  sel.innerHTML = '<option value="">All racks</option>' +
    racks.map((r) => `<option value="${r.id}">${esc(r.code)} · ${esc(r.name)}</option>`).join('');
  sel.value = current;
}

function renderTape(events) {
  const tape = $('#tape');
  if (!events || !events.length) {
    tape.innerHTML = '<div class="empty-note">Nothing has moved yet. Simulated or real rack readings appear here.</div>';
    return;
  }
  tape.innerHTML = events.map((e) => {
    const tagClass = e.kind === 'low_stock' ? 'low_stock' : e.kind;
    const amount = e.kind === 'restock' ? `+${kg(e.delta_kg)} kg`
      : e.delta_kg < 0 ? `−${kg(Math.abs(e.delta_kg))} kg` : '—';
    return `
      <div class="tape-row">
        <span class="t">${esc(e.time.split(' ').slice(1).join(' ') || hhmm(e.ts))}</span>
        <span class="what"><b>${esc(e.rack_code)}</b> <span>${esc(e.note || '')}</span></span>
        <span class="tag ${esc(tagClass)}">${esc(e.status)} · ${amount}${e.qty ? ' · ~' + e.qty + ' pcs' : ''}</span>
      </div>`;
  }).join('');
}

function renderDevices(racks) {
  if (!racks) return;
  $('#deviceCount').textContent = `${racks.length} controllers`;
  $('#deviceGrid').innerHTML = racks.map((r) => `
    <div class="device">
      <h4>${esc(r.code)} · ${esc(r.name)}</h4>
      <div class="sub">${r.online ? 'Reporting' : 'Last seen ' + ago(r.last_seen)} · ${r.cells_g.length} load cells</div>
      <div class="key-row">
        <code title="${esc(r.device_key)}">${esc(r.device_key)}</code>
        <button class="btn btn-sm btn-ghost" data-action="copykey" data-key="${esc(r.device_key)}">Copy</button>
      </div>
      <dl>
        <dt>Mode</dt><dd>${r.armed ? 'armed' : 'selling'}</dd>
        <dt>Capacity</dt><dd>${kg(r.capacity_kg, 1)} kg</dd>
        <dt>Tare</dt><dd>${kg(r.tare_kg, 2)} kg</dd>
        <dt>Avg item</dt><dd>${Math.round(r.avg_item_g)} g</dd>
        <dt>Last reading</dt><dd>${ago(r.last_seen)}</dd>
      </dl>
    </div>`).join('');
}

/* ----------------------------------------------------------------- chart -- */
async function loadChart() {
  try {
    const { points, tz } = await api(`/api/series?minutes=${state.range}`);
    if (tz) state.tz = tz;
    drawChart(points || []);
  } catch (e) { /* ignored, chart is decorative when offline */ }
}

function drawChart(points) {
  const host = $('#chart');
  host.innerHTML = '';
  if (!points.length) {
    host.innerHTML = '<div class="chart-empty">No readings in this window yet.</div>';
    return;
  }
  const W = 620, H = 178, padL = 34, padR = 8, padT = 10, padB = 22;
  const values = points.map((p) => p.kg);
  let min = Math.min(...values), max = Math.max(...values);
  if (max - min < 1) { max += 1; min -= 1; }
  const span = max - min;
  min = Math.max(0, min - span * 0.15);
  max = max + span * 0.15;
  const x = (i) => padL + (i / Math.max(1, points.length - 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - min) / (max - min)) * (H - padT - padB);

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.kg).toFixed(1)}`).join(' ');
  const area = `${line} L${x(points.length - 1).toFixed(1)},${H - padB} L${x(0).toFixed(1)},${H - padB} Z`;

  const ticks = 3;
  let grid = '';
  for (let i = 0; i <= ticks; i++) {
    const v = min + ((max - min) * i) / ticks;
    const yy = y(v);
    grid += `<line x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}" stroke="#d0d7cc" stroke-width="1" stroke-dasharray="${i === 0 ? '0' : '3 4'}"/>
      <text x="${padL - 6}" y="${yy + 3.5}" text-anchor="end" font-size="9" fill="#5c6b63" font-family="JetBrains Mono, monospace">${v.toFixed(0)}</text>`;
  }
  const last = points[points.length - 1];
  const firstTime = hhmm(points[0].ts);
  const lastTime = hhmm(last.ts);

  host.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Total shelf load, ${kg(last.kg, 1)} kilograms now">
      <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#1e5b4c" stop-opacity="0.22"/>
        <stop offset="100%" stop-color="#1e5b4c" stop-opacity="0"/>
      </linearGradient></defs>
      ${grid}
      <path d="${area}" fill="url(#fill)"/>
      <path d="${line}" fill="none" stroke="#1e5b4c" stroke-width="1.8" stroke-linejoin="round"/>
      <circle cx="${x(points.length - 1)}" cy="${y(last.kg)}" r="3.2" fill="#1e5b4c" stroke="#f7f8f4" stroke-width="1.5"/>
      <text x="${padL}" y="${H - 6}" font-size="9" fill="#5c6b63" font-family="JetBrains Mono, monospace">${firstTime}</text>
      <text x="${W - padR}" y="${H - 6}" text-anchor="end" font-size="9" fill="#5c6b63" font-family="JetBrains Mono, monospace">${lastTime}</text>
    </svg>`;
  $('#chartSub').textContent = `${kg(last.kg, 1)} kg now · last ${state.range >= 1440 ? '24 hours' : state.range + ' min'}`;
}

$$('#rangeGroup button').forEach((b) => b.addEventListener('click', () => {
  $$('#rangeGroup button').forEach((o) => o.classList.remove('active'));
  b.classList.add('active');
  state.range = Number(b.dataset.min);
  loadChart();
}));

setInterval(() => { if (state.view === 'dashboard') loadChart(); }, 45000);

/* ------------------------------------------------------------------- log -- */
let logTimer = null;
function scheduleLogReload() {
  clearTimeout(logTimer);
  logTimer = setTimeout(loadLog, 400);
}

async function loadLog() {
  const rack = $('#logRack').value;
  const kind = $('#logKind').value;
  const q = $('#logSearch').value.trim();
  const params = new URLSearchParams({ limit: '300' });
  if (rack) params.set('rack_id', rack);
  if (kind) params.set('kind', kind);
  if (q) params.set('q', q);
  try {
    const { events } = await api('/api/events?' + params.toString());
    $('#logBody').innerHTML = events.length ? events.map((e) => `
      <tr>
        <td class="num">${esc(e.time)}</td>
        <td>${esc(e.rack_code)}</td>
        <td class="num">${kg(e.prev_kg)}</td>
        <td class="num">${kg(e.curr_kg)}</td>
        <td class="num">${e.delta_kg > 0 ? '+' : ''}${kg(e.delta_kg)} kg</td>
        <td class="num">${e.qty ?? '—'}</td>
        <td class="status ${esc(e.kind === 'low_stock' ? 'low' : e.kind)}">${esc(e.status)}</td>
      </tr>`).join('')
      : '<tr><td colspan="7" style="padding:2rem 1.1rem;color:var(--muted)">No movements match these filters.</td></tr>';
  } catch (e) {
    toast(e.message, true);
  }
}

['#logRack', '#logKind'].forEach((s) => $(s).addEventListener('change', loadLog));
$('#logSearch').addEventListener('input', () => { clearTimeout(logTimer); logTimer = setTimeout(loadLog, 350); });

async function download(url, name) {
  try {
    const res = await fetch(url, { headers: { Authorization: `Bearer ${state.token}` } });
    if (!res.ok) throw new Error('Export failed');
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast('Export downloaded');
  } catch (e) {
    toast(e.message, true);
  }
}

function exportParams() {
  const p = new URLSearchParams();
  if ($('#logRack').value) p.set('rack_id', $('#logRack').value);
  if ($('#logKind').value) p.set('kind', $('#logKind').value);
  return p.toString();
}

$('#exportXlsx').addEventListener('click', () => {
  const stamp = new Date().toISOString().slice(0, 10);
  download('/api/export.xlsx?' + exportParams(), `RackSense inventory log ${stamp}.xlsx`);
});
$('#exportCsv').addEventListener('click', () => {
  const stamp = new Date().toISOString().slice(0, 10);
  download('/api/export.csv?' + exportParams(), `RackSense inventory log ${stamp}.csv`);
});

/* ---------------------------------------------------------------- alerts -- */
async function loadAlertHistory() {
  try {
    const { alerts } = await api('/api/alerts?state=all&limit=80');
    if (!alerts.length) {
      $('#alertHistory').innerHTML = '<div class="empty-note">No alerts recorded yet.</div>';
      return;
    }
    const rows = alerts.map((a) => `
      <tr>
        <td class="num">${esc(a.time)}</td>
        <td>${esc(a.rack_code)}</td>
        <td style="white-space:normal">${esc(a.message)}</td>
        <td>${a.ack_by ? esc(a.ack_by) : '—'}</td>
        <td class="status ${a.state === 'open' || a.state === 'acknowledged' ? 'theft' : 'sale'}">${a.state === 'open' ? 'Open' : a.state === 'acknowledged' ? 'Acknowledged' : 'Resolved'}</td>
        <td>
          ${a.state === 'open' ? `<button class="btn btn-sm" data-action="ack" data-id="${a.id}">Acknowledge</button>` : ''}
          ${a.state !== 'resolved' ? `<button class="btn btn-sm btn-danger" data-action="resolve" data-id="${a.id}">Resolve</button>` : ''}
        </td>
      </tr>`).join('');
    $('#alertHistory').innerHTML = `
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>Date &amp; time</th><th>Rack</th><th>Alert</th><th>Acknowledged by</th><th>State</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  } catch (e) {
    toast(e.message, true);
  }
}

$('#clearResolvedBtn').addEventListener('click', () => {
  if (state.view !== 'alerts') setView('alerts');
  toast('Resolved alerts are kept in Alert history');
});

/* --------------------------------------------------------------- actions -- */
document.addEventListener('click', async (e) => {
  const menuBtn = e.target.closest('[data-menu]');
  if (menuBtn) {
    const id = menuBtn.dataset.menu;
    const menu = $(`#menu-${id}`);
    const wasOpen = menu.classList.contains('open');
    $$('.dropdown-menu').forEach((m) => m.classList.remove('open'));
    if (!wasOpen) { menu.classList.add('open'); state.menuOpen = id; }
    else { state.menuOpen = null; }
    e.stopPropagation();
    return;
  }
  if (!e.target.closest('.dropdown-menu')) {
    $$('.dropdown-menu').forEach((m) => m.classList.remove('open'));
    state.menuOpen = null;
  }

  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const id = btn.dataset.id;

  try {
    if (action === 'sim') {
      const out = await api(`/api/racks/${id}/simulate`, {
        method: 'POST', body: JSON.stringify({ action: btn.dataset.kind }),
      });
      toast(out.events?.length ? `${out.events[0].status} on ${out.events[0].rack_code}` : 'Reading processed — no stable change');
    } else if (action === 'tare') {
      const out = await api(`/api/racks/${id}/tare`, { method: 'POST' });
      toast(`Baseline saved at ${kg(out.weight_kg, 2)} kg`);
    } else if (action === 'arm') {
      const next = btn.dataset.armed !== '1';
      await api(`/api/racks/${id}`, { method: 'PATCH', body: JSON.stringify({ armed: next }) });
      toast(next ? 'Rack armed — removals now raise alerts' : 'Rack disarmed — removals logged as sales');
    } else if (action === 'edit') {
      openRackModal(id);
    } else if (action === 'delete') {
      openDeleteModal(id);
    } else if (action === 'ack') {
      await api(`/api/alerts/${id}/ack`, { method: 'POST' });
      toast('Alert acknowledged');
    } else if (action === 'resolve') {
      await api(`/api/alerts/${id}/resolve`, { method: 'POST' });
      toast('Alert resolved');
    } else if (action === 'copykey') {
      await navigator.clipboard.writeText(btn.dataset.key);
      toast('Device key copied');
    } else if (action === 'addrack') {
      openRackModal(null);
    }
  } catch (err) {
    toast(err.message, true);
  }
  await refresh();
});

$('#addRackBtn').addEventListener('click', () => openRackModal(null));
$('#simSwitch').addEventListener('click', async () => {
  const next = $('#simSwitch').getAttribute('aria-checked') !== 'true';
  try {
    await api('/api/sim', { method: 'POST', body: JSON.stringify({ enabled: next }) });
    $('#simSwitch').setAttribute('aria-checked', String(next));
    toast(next ? 'Simulated hardware running' : 'Simulated hardware paused');
  } catch (e) { toast(e.message, true); }
});

/* ----------------------------------------------------------------- modal -- */
function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modalBody').innerHTML = '';
  $('#modalFoot').innerHTML = '';
}
$('#modalClose').addEventListener('click', closeModal);
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

function openRackModal(id) {
  const rack = state.snap?.racks.find((r) => String(r.id) === String(id));
  $('#modalTitle').textContent = rack ? `Edit ${rack.code}` : 'Add a rack';
  $('#modalBody').innerHTML = `
    <div class="form-grid">
      <div class="field"><label>Rack code</label><input id="f-code" type="text" value="${rack ? esc(rack.code) : ''}" placeholder="R-07" ${rack && state.user.role !== 'owner' ? 'disabled' : ''}></div>
      <div class="field"><label>Name</label><input id="f-name" type="text" value="${rack ? esc(rack.name) : ''}" placeholder="Onion & potato"></div>
      <div class="field"><label>Location</label><input id="f-location" type="text" value="${rack ? esc(rack.location) : ''}" placeholder="Aisle 2 · Window"></div>
      <div class="field"><label>Tier</label><input id="f-tier" type="text" value="${rack ? esc(rack.tier) : ''}" placeholder="Tier 1"></div>
      <div class="field"><label>Capacity (kg)</label><input id="f-capacity" type="number" step="0.5" min="1" value="${rack ? rack.capacity_kg : 25}"></div>
      <div class="field"><label>Tare (kg)</label><input id="f-tare" type="number" step="0.1" min="0" value="${rack ? rack.tare_kg : 0}"></div>
      <div class="field"><label>Average item (g)</label><input id="f-avg" type="number" step="10" min="1" value="${rack ? Math.round(rack.avg_item_g) : 200}"></div>
      <div class="field"><label>Low-stock threshold (%)</label><input id="f-low" type="number" step="1" min="1" max="90" value="${rack ? rack.low_stock_pct : 20}"></div>
      <div class="span-2" style="font-size:.8rem;color:var(--muted)">${rack ? 'Device key <code class="mono">' + esc(rack.device_key) + '</code> stays the same.' : 'A device key is generated automatically for the ESP32 controller.'}</div>
    </div>`;
  $('#modalFoot').innerHTML = `
    <button class="btn" id="m-cancel">Cancel</button>
    <button class="btn btn-primary" id="m-save">${rack ? 'Save rack' : 'Create rack'}</button>`;
  $('#modal').classList.remove('hidden');
  $('#m-cancel').onclick = closeModal;
  $('#m-save').onclick = async () => {
    const body = {
      code: $('#f-code').value.trim(),
      name: $('#f-name').value.trim(),
      location: $('#f-location').value.trim(),
      tier: $('#f-tier').value.trim(),
      capacity_kg: Number($('#f-capacity').value),
      tare_kg: Number($('#f-tare').value),
      avg_item_g: Number($('#f-avg').value),
      low_stock_pct: Number($('#f-low').value),
    };
    if (!body.name.trim()) { toast('Give the rack a name', true); return; }
    try {
      if (rack) await api(`/api/racks/${rack.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      else await api('/api/racks', { method: 'POST', body: JSON.stringify(body) });
      closeModal();
      toast(rack ? 'Rack updated' : 'Rack added');
      await refresh();
    } catch (e) { toast(e.message, true); }
  };
}

function openDeleteModal(id) {
  const rack = state.snap?.racks.find((r) => String(r.id) === String(id));
  if (!rack) return;
  $('#modalTitle').textContent = `Remove ${rack.code}?`;
  $('#modalBody').innerHTML = `<p style="color:var(--muted)">The rack, its readings and its alert history will be deleted. This cannot be undone.</p>`;
  $('#modalFoot').innerHTML = `<button class="btn" id="m-cancel">Keep it</button><button class="btn btn-danger" id="m-del">Remove rack</button>`;
  $('#modal').classList.remove('hidden');
  $('#m-cancel').onclick = closeModal;
  $('#m-del').onclick = async () => {
    try {
      await api(`/api/racks/${id}`, { method: 'DELETE' });
      closeModal();
      toast('Rack removed');
      await refresh();
    } catch (e) { toast(e.message, true); }
  };
}

/* ------------------------------------------------------------------ boot -- */
setInterval(() => {
  $('#clock').textContent = new Date().toLocaleTimeString('en-GB', {
    timeZone: state.tz || 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
}, 1000);

if (state.token) {
  api('/api/auth/me')
    .then(({ user }) => { state.user = user; start(); })
    .catch(() => signOut());
} else {
  $('#login').classList.remove('hidden');
}
