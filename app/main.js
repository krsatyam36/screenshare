const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const https = require('https');

const { ServerController } = require('./lib/serverctl');
const history = require('./lib/history');
const settings = require('./lib/settings');

const ICON = path.join(__dirname, 'build', 'icon.png');
let win = null;
let tray = null;
let quitting = false;

const server = new ServerController();
let sessionId = null;
let sessionLog = [];
let sessionError = false;

// If SQLite is unavailable, replace history with safe no-ops so the rest of
// the app keeps working (the control panel still starts/stops the server).
function historyDisable() {
  const noop = () => {};
  history.startSession = () => null;
  history.endSession = noop;
  history.syncConnections = noop;
  history.listSessions = () => [];
  history.getSession = () => null;
  history.meta = () => ({ first_opened: null, last_opened: null, total_sessions: 0 });
}

// ── Window ───────────────────────────────────────────────────────────────────
function createWindow() {
  win = new BrowserWindow({
    width: 940, height: 680, minWidth: 820, minHeight: 560,
    backgroundColor: '#0d0e12',
    title: 'Screen Stream',
    icon: fs.existsSync(ICON) ? ICON : undefined,
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  win.on('close', (e) => {
    if (quitting || !server.isRunning()) return;   // normal close
    e.preventDefault();
    win.webContents.send('confirm-close');          // renderer shows the modal
  });
}

// ── Tray ───────────────────────────────────────────────────────────────────
function buildTray() {
  try {
    const img = fs.existsSync(ICON)
      ? nativeImage.createFromPath(ICON).resize({ width: 22, height: 22 })
      : nativeImage.createEmpty();
    tray = new Tray(img);
    tray.setToolTip('Screen Stream');
    refreshTray();
    tray.on('click', () => showWindow());
  } catch (_) { /* tray optional */ }
}

function refreshTray() {
  if (!tray) return;
  const running = server.isRunning();
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: running ? '● Streaming' : '○ Stopped', enabled: false },
    { type: 'separator' },
    { label: 'Show window', click: () => showWindow() },
    running ? { label: 'Stop', click: () => server.stop() }
            : { label: 'Start', click: () => doStart() },
    { type: 'separator' },
    { label: 'Quit', click: () => { quitting = true; server.stop(); setTimeout(() => app.quit(), 300); } },
  ]));
}

function showWindow() { if (!win) createWindow(); else { win.show(); win.focus(); } }

// ── Server lifecycle wiring ──────────────────────────────────────────────────
function doStart() {
  const s = settings.read();
  server.start({ tls: s.tls, fixedPin: s.pinMode === 'fixed' ? s.fixedPin : null });
}

server.on('log', ({ line, level }) => {
  sessionLog.push(`${line}`);
  if (sessionLog.length > 5000) sessionLog.shift();
  if (level === 'error') sessionError = true;
  if (win) win.webContents.send('log', { line, level, ts: Date.now() });
});
server.on('url', (url) => { if (win) win.webContents.send('url', url); });
server.on('status', (st) => {
  if (sessionId) history.syncConnections(sessionId, st.clients);
  if (win) win.webContents.send('status', st);
});
server.on('state', (info) => {
  if (info.running && sessionId === null) {
    sessionLog = []; sessionError = false;
    sessionId = history.startSession(info.tls);
  }
  if (win) win.webContents.send('state', info);
  refreshTray();
});
server.on('exit', () => {
  if (sessionId !== null) {
    history.endSession(sessionId, { log: sessionLog.join('\n'), error: sessionError });
    sessionId = null;
  }
});

// ── IPC ───────────────────────────────────────────────────────────────────
ipcMain.handle('server:start', () => { doStart(); return server.info(); });
ipcMain.handle('server:stop', () => { server.stop(); return true; });
ipcMain.handle('server:info', () => server.info());
ipcMain.handle('settings:get', () => settings.read());
ipcMain.handle('settings:set', (_e, patch) => {
  const next = settings.write(patch);
  if ('autostart' in patch) applyAutostart(next.autostart);
  return next;
});
ipcMain.handle('history:list', () => history.listSessions());
ipcMain.handle('history:get', (_e, id) => history.getSession(id));
ipcMain.handle('history:meta', () => history.meta());
ipcMain.handle('app:openExternal', (_e, url) => shell.openExternal(url));
ipcMain.handle('app:version', () => app.getVersion());
ipcMain.handle('qr:generate', async (_e, url) => {
  try { return await require('qrcode').toDataURL(url, { margin: 1, width: 320 }); }
  catch (_) { return null; }
});
ipcMain.handle('updates:check', () => checkUpdates());

ipcMain.on('app:close-choice', (_e, choice) => {
  if (choice === 'quit') { quitting = true; server.stop(); setTimeout(() => app.quit(), 300); }
  else if (win) win.hide();   // 'tray'
});

// ── Autostart (~/.config/autostart) ──────────────────────────────────────────
function applyAutostart(on) {
  const dir = path.join(os.homedir(), '.config', 'autostart');
  const file = path.join(dir, 'screen-stream-app.desktop');
  try {
    if (on) {
      fs.mkdirSync(dir, { recursive: true });
      const exec = app.isPackaged ? process.execPath : `bash -c 'cd ${path.join(__dirname, '..')} && npm --prefix app start'`;
      fs.writeFileSync(file, `[Desktop Entry]\nType=Application\nName=Screen Stream\nExec=${exec}\nIcon=screenshare\nX-GNOME-Autostart-enabled=true\n`);
    } else if (fs.existsSync(file)) {
      fs.unlinkSync(file);
    }
  } catch (_) {}
}

// ── Update check (GitHub latest release) ──────────────────────────────────────
function checkUpdates() {
  return new Promise((resolve) => {
    const opts = { host: 'api.github.com', path: '/repos/krsatyam36/screenshare/releases/latest',
                   headers: { 'User-Agent': 'screen-stream-app' }, timeout: 6000 };
    const req = https.get(opts, (res) => {
      let body = '';
      res.on('data', (d) => (body += d));
      res.on('end', () => {
        try {
          const j = JSON.parse(body);
          const latest = (j.tag_name || '').replace(/^v/, '');
          const current = app.getVersion();
          resolve({ current, latest, url: j.html_url || '', newer: !!latest && latest !== current });
        } catch (_) { resolve({ current: app.getVersion(), latest: null, error: 'parse' }); }
      });
    });
    req.on('error', () => resolve({ current: app.getVersion(), latest: null, error: 'network' }));
    req.on('timeout', () => { req.destroy(); resolve({ current: app.getVersion(), latest: null, error: 'timeout' }); });
  });
}

// ── App lifecycle ─────────────────────────────────────────────────────────────
const single = app.requestSingleInstanceLock();
if (!single) { app.quit(); }
else {
  app.on('second-instance', () => showWindow());
  app.whenReady().then(() => {
    try { history.init(); history.recordAppOpen(); }
    catch (e) { console.warn('history disabled:', e.message); historyDisable(); }
    createWindow();
    buildTray();
    app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
  });
  app.on('window-all-closed', (e) => { /* keep alive for tray */ });
  app.on('before-quit', () => { quitting = true; try { server.stop(); } catch (_) {} });
}
