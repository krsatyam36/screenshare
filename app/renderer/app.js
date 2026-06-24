(function () {
"use strict";
const $ = (s) => document.querySelector(s);
const api = window.api;

let state = { running: false, pin: null, url: null, tls: true };
let connected = 0;

// ── Navigation ───────────────────────────────────────────────────────────────
document.querySelectorAll('.nav-item').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach((x) => x.classList.remove('active'));
    document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
    b.classList.add('active');
    $(`#view-${b.dataset.view}`).classList.add('active');
    if (b.dataset.view === 'history') loadHistory();
  });
});

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString() : '—';
const fmtDur = (s) => {
  if (!s || s < 0) return '—';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
};

function friendly() {
  if (!state.running) return 'Idle — press Start to begin.';
  if (connected === 0) return 'Streaming — waiting for a tablet to connect…';
  return `Streaming — ${connected} device${connected > 1 ? 's' : ''} connected.`;
}

// ── Render server state ───────────────────────────────────────────────────────
async function renderState(info) {
  state = info;
  document.body.classList.toggle('running', !!info.running);
  $('#state-text').textContent = info.running ? 'Streaming' : 'Stopped';
  $('#side-state').textContent = info.running ? 'Streaming' : 'Stopped';
  $('#state-pill').querySelector('.dot').classList.toggle('on', !!info.running);
  $('#side-dot').classList.toggle('on', !!info.running);
  $('#start-btn').disabled = info.running;
  $('#stop-btn').disabled = !info.running;

  if (info.running) {
    $('#pin').textContent = info.pin || '––––––';
    if (info.url) setUrl(info.url);
  } else {
    $('#pin').textContent = '––––––';
    $('#url').textContent = '—';
    $('#qr').style.display = 'none';
    $('#qr-empty').style.display = 'block';
    connected = 0;
    setConn(0);
  }
  $('#friendly').textContent = friendly();
}

async function setUrl(url) {
  state.url = url;
  $('#url').textContent = url;
  const data = await api.qr(url);
  if (data) { $('#qr').src = data; $('#qr').style.display = 'block'; $('#qr-empty').style.display = 'none'; }
}

function setConn(n) {
  connected = n;
  const chip = $('#conn-count');
  chip.textContent = `${n} device${n === 1 ? '' : 's'}`;
  chip.className = 'chip ' + (n > 0 ? 'chip-live' : 'chip-idle');
  $('#friendly').textContent = friendly();
}

// ── Log console ───────────────────────────────────────────────────────────────
const logEl = $('#log');
function appendLog({ line, level, ts }) {
  const div = document.createElement('div');
  div.className = 'l-' + (level || 'info');
  const t = new Date(ts || Date.now()).toLocaleTimeString();
  div.innerHTML = `<span class="ts"></span>`;
  div.querySelector('.ts').textContent = t;
  div.appendChild(document.createTextNode(line));
  logEl.appendChild(div);
  while (logEl.children.length > 2000) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
}
$('#toggle-raw').addEventListener('click', () => {
  const hidden = logEl.classList.toggle('hidden');
  $('#toggle-raw').textContent = hidden ? 'Show raw logs' : 'Hide raw logs';
});
$('#clear-log').addEventListener('click', () => { logEl.innerHTML = ''; });
$('#copy-log').addEventListener('click', () => {
  navigator.clipboard.writeText([...logEl.children].map((c) => c.textContent).join('\n'));
  flash('#copy-log', 'Copied');
});

// ── Buttons ───────────────────────────────────────────────────────────────────
$('#start-btn').addEventListener('click', () => api.startServer());
$('#stop-btn').addEventListener('click', () => api.stopServer());
$('#open-url').addEventListener('click', () => state.url && api.openExternal(state.url));
document.querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click', () => {
  const v = b.dataset.copy === 'pin' ? state.pin : state.url;
  if (v) { navigator.clipboard.writeText(v); flash(b, '✓'); }
}));
function flash(sel, txt) {
  const el = typeof sel === 'string' ? $(sel) : sel;
  const old = el.textContent; el.textContent = txt;
  setTimeout(() => (el.textContent = old), 1000);
}

// ── Pushed events ─────────────────────────────────────────────────────────────
api.on('state', (info) => renderState(info));
api.on('log', (entry) => appendLog(entry));
api.on('url', (url) => setUrl(url));
api.on('status', (st) => setConn(st.active || 0));
api.on('confirm-close', () => $('#close-modal').classList.add('show'));
$('#close-tray').addEventListener('click', () => { $('#close-modal').classList.remove('show'); api.closeChoice('tray'); });
$('#close-quit').addEventListener('click', () => api.closeChoice('quit'));

// ── History ───────────────────────────────────────────────────────────────────
async function loadHistory() {
  const m = await api.meta();
  $('#meta-row').innerHTML = `
    <div class="meta-card"><div class="m-val">${m.total_sessions || 0}</div><div class="m-lab">Sessions</div></div>
    <div class="meta-card"><div class="m-val">${m.first_opened ? fmtTime(m.first_opened) : '—'}</div><div class="m-lab">First opened</div></div>
    <div class="meta-card"><div class="m-val">${m.last_opened ? fmtTime(m.last_opened) : '—'}</div><div class="m-lab">Last opened</div></div>`;
  const sessions = await api.listHistory();
  const list = $('#sessions');
  list.innerHTML = '';
  if (!sessions.length) { list.innerHTML = '<div class="muted" style="padding:8px">No sessions yet.</div>'; return; }
  for (const s of sessions) {
    const row = document.createElement('div');
    row.className = 's-row';
    row.innerHTML = `<div class="s-when"></div>
      <div class="s-sub"><span>${fmtDur(s.duration)}</span><span>${s.conns} conn</span>${s.error ? '<span class="badge-err">⚠ error</span>' : ''}</div>`;
    row.querySelector('.s-when').textContent = fmtTime(s.started_at);
    row.addEventListener('click', () => { document.querySelectorAll('.s-row').forEach(r=>r.classList.remove('sel')); row.classList.add('sel'); showSession(s.id); });
    list.appendChild(row);
  }
}
async function showSession(id) {
  const s = await api.getSession(id);
  const d = $('#session-detail');
  if (!s) { d.innerHTML = '<div class="muted">Not found.</div>'; return; }
  const conns = (s.connections || []).map((c) =>
    `<div class="s-sub"><span>${c.ip}</span><span>${c.screen}</span><span>${fmtTime(c.connected_at)}</span><span>${fmtDur(c.duration)}</span></div>`).join('') || '<div class="muted">No connections.</div>';
  d.innerHTML = `<h3>${fmtTime(s.started_at)}</h3>
    <div class="s-sub" style="margin:8px 0"><span>Duration ${fmtDur(s.duration)}</span><span>TLS ${s.tls ? 'on' : 'off'}</span>${s.error ? '<span class="badge-err">had errors</span>' : ''}</div>
    <div><strong>Connections</strong>${conns}</div>
    <strong>Log</strong><pre></pre>`;
  d.querySelector('pre').textContent = s.log || '(empty)';
}

// ── Advanced / settings ────────────────────────────────────────────────────────
async function loadSettings() {
  const s = await api.getSettings();
  $('#pin-mode').value = s.pinMode;
  $('#fixed-pin').value = s.fixedPin || '';
  $('#fixed-pin').style.display = s.pinMode === 'fixed' ? '' : 'none';
  $('#tls').checked = s.tls;
  $('#autostart').checked = s.autostart;
}
$('#pin-mode').addEventListener('change', (e) => {
  $('#fixed-pin').style.display = e.target.value === 'fixed' ? '' : 'none';
  api.setSettings({ pinMode: e.target.value });
});
$('#fixed-pin').addEventListener('change', (e) => api.setSettings({ fixedPin: e.target.value.trim() }));
$('#tls').addEventListener('change', (e) => api.setSettings({ tls: e.target.checked }));
$('#autostart').addEventListener('change', (e) => api.setSettings({ autostart: e.target.checked }));
$('#check-updates').addEventListener('click', async () => {
  $('#update-status').textContent = 'Checking…';
  const r = await api.checkUpdates();
  if (r.error) $('#update-status').textContent = `Couldn't check (${r.error}).`;
  else if (r.newer) $('#update-status').innerHTML = `Update available: v${r.latest} (you have v${r.current}).`;
  else $('#update-status').textContent = `You're up to date (v${r.current}).`;
});

// ── Init ───────────────────────────────────────────────────────────────────────
(async () => {
  $('#side-ver').textContent = 'v' + (await api.appVersion());
  $('#db-path').textContent = 'History stored in ~/.config/screen-stream/history.db';
  await loadSettings();
  renderState(await api.serverInfo());
})();
})();
