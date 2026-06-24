// Session/connection history in SQLite at ~/.config/screen-stream/history.db
// (same dir the server uses, so everything lives together).
const os = require('os');
const path = require('path');
const fs = require('fs');

const DIR = path.join(os.homedir(), '.config', 'screen-stream');
const DB_PATH = path.join(DIR, 'history.db');

let db = null;
let openConns = new Map(); // key -> connection row id (for the active session)

function init() {
  fs.mkdirSync(DIR, { recursive: true });
  const Database = require('better-sqlite3');
  db = new Database(DB_PATH);
  db.pragma('journal_mode = WAL');
  db.exec(`
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at REAL, ended_at REAL, duration REAL,
      tls INTEGER, error INTEGER DEFAULT 0, log TEXT
    );
    CREATE TABLE IF NOT EXISTS connections (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id INTEGER, ip TEXT, screen TEXT,
      connected_at REAL, disconnected_at REAL, duration REAL
    );
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
  `);
  return db;
}

function recordAppOpen() {
  const now = Date.now() / 1000;
  const get = db.prepare('SELECT value FROM meta WHERE key=?');
  const set = db.prepare('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value');
  if (!get.get('first_opened')) set.run('first_opened', String(now));
  set.run('last_opened', String(now));
  return {
    first: parseFloat(get.get('first_opened')?.value || now),
    last: now,
  };
}

function startSession(tls) {
  openConns = new Map();
  const r = db.prepare('INSERT INTO sessions(started_at, tls) VALUES(?,?)')
              .run(Date.now() / 1000, tls ? 1 : 0);
  return r.lastInsertRowid;
}

function endSession(sessionId, { log, error }) {
  const now = Date.now() / 1000;
  const s = db.prepare('SELECT started_at FROM sessions WHERE id=?').get(sessionId);
  const dur = s ? now - s.started_at : 0;
  db.prepare('UPDATE sessions SET ended_at=?, duration=?, log=?, error=? WHERE id=?')
    .run(now, dur, log || '', error ? 1 : 0, sessionId);
  // close any still-open connections
  for (const id of openConns.values()) {
    const c = db.prepare('SELECT connected_at FROM connections WHERE id=?').get(id);
    if (c) db.prepare('UPDATE connections SET disconnected_at=?, duration=? WHERE id=?')
             .run(now, now - c.connected_at, id);
  }
  openConns = new Map();
}

// Diff the live client list against tracked connections.
function syncConnections(sessionId, clients) {
  const now = Date.now() / 1000;
  const seen = new Set();
  for (const c of clients || []) {
    const key = `${c.ip}|${c.screen}|${c.since}`;
    seen.add(key);
    if (!openConns.has(key)) {
      const r = db.prepare('INSERT INTO connections(session_id, ip, screen, connected_at) VALUES(?,?,?,?)')
                  .run(sessionId, c.ip, c.screen, c.since || now);
      openConns.set(key, r.lastInsertRowid);
    }
  }
  for (const [key, id] of [...openConns.entries()]) {
    if (!seen.has(key)) {
      const c = db.prepare('SELECT connected_at FROM connections WHERE id=?').get(id);
      if (c) db.prepare('UPDATE connections SET disconnected_at=?, duration=? WHERE id=?')
               .run(now, now - c.connected_at, id);
      openConns.delete(key);
    }
  }
}

function listSessions(limit = 100) {
  const rows = db.prepare(`
    SELECT s.*, (SELECT COUNT(*) FROM connections c WHERE c.session_id=s.id) AS conns
    FROM sessions s ORDER BY s.started_at DESC LIMIT ?`).all(limit);
  return rows;
}

function getSession(id) {
  const s = db.prepare('SELECT * FROM sessions WHERE id=?').get(id);
  if (!s) return null;
  s.connections = db.prepare('SELECT * FROM connections WHERE session_id=? ORDER BY connected_at').all(id);
  return s;
}

function meta() {
  const get = db.prepare('SELECT value FROM meta WHERE key=?');
  return {
    first_opened: parseFloat(get.get('first_opened')?.value || 0) || null,
    last_opened: parseFloat(get.get('last_opened')?.value || 0) || null,
    total_sessions: db.prepare('SELECT COUNT(*) n FROM sessions').get().n,
  };
}

module.exports = {
  init, recordAppOpen, startSession, endSession, syncConnections,
  listSessions, getSession, meta, DB_PATH,
};
