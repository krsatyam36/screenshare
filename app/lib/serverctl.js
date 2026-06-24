// Manages the Python Screen Stream server as a child process: generates the
// PIN, launches it, streams its logs, and polls /status for live connections.
const { app } = require('electron');
const { spawn } = require('child_process');
const { EventEmitter } = require('events');
const crypto = require('crypto');
const https = require('https');
const path = require('path');
const fs = require('fs');

const ALNUM = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'; // no look-alikes

function genPin(len = 6) {
  const b = crypto.randomBytes(len);
  let s = '';
  for (let i = 0; i < len; i++) s += ALNUM[b[i] % ALNUM.length];
  return s;
}

function resourceBase() {
  return app.isPackaged ? process.resourcesPath : path.join(__dirname, '..', '..');
}

class ServerController extends EventEmitter {
  constructor() {
    super();
    this.child = null;
    this.running = false;
    this.starting = false;
    this.pin = null;
    this.url = null;
    this.tls = true;
    this._poll = null;
    this._lastActive = -1;
  }

  isRunning() { return this.running; }

  info() {
    return { running: this.running, pin: this.pin, url: this.url, tls: this.tls };
  }

  // opts: { tls: bool, fixedPin: string|null }
  // Async so the one-time venv setup (packaged) never blocks the UI thread.
  async start(opts = {}) {
    if (this.running || this.starting) return;
    this.starting = true;
    this.tls = opts.tls !== false;
    this.pin = opts.fixedPin && opts.fixedPin.length ? opts.fixedPin : genPin();
    this.url = null;

    const base = resourceBase();
    const args = ['--pin', this.pin, this.tls ? '--tls' : '--no-tls'];
    try {
      let cmd, cmdArgs;
      const env = { ...process.env };
      if (app.isPackaged) {
        const py = await this._ensureVenv();      // async — keeps the UI alive
        if (!py) return;                          // setup failed; logged already
        cmd = py;
        cmdArgs = ['-m', 'screenshare', ...args];
        env.PYTHONPATH = path.join(base, 'src');
      } else {
        cmd = 'bash';                             // dev: start.sh handles venv/assets
        cmdArgs = [path.join(base, 'start.sh'), ...args];
      }
      this._spawn(cmd, cmdArgs, base, env);
    } finally {
      this.starting = false;
    }
  }

  _spawn(cmd, cmdArgs, cwd, env) {
    this.emit('log', { line: `$ ${path.basename(cmd)} ${cmdArgs.join(' ')}`, level: 'cmd' });
    this.child = spawn(cmd, cmdArgs, { cwd, env });
    this.running = true;
    this.emit('state', this.info());

    const onData = (buf, stream) => {
      for (const raw of buf.toString().split('\n')) {
        const line = raw.replace(/\r$/, '');
        if (!line.trim()) continue;
        let level = 'info';
        if (/\[ERROR\]|Traceback|error:/i.test(line)) level = 'error';
        else if (/\[WARNING\]|warn/i.test(line)) level = 'warn';
        else if (stream === 'err' && /error|fail/i.test(line)) level = 'error';
        const m = line.match(/Tablet URL\s+→\s+(\S+)/);
        if (m) { this.url = m[1]; this.emit('url', this.url); }
        this.emit('log', { line, level });
      }
    };
    this.child.stdout.on('data', (b) => onData(b, 'out'));
    this.child.stderr.on('data', (b) => onData(b, 'err'));

    this.child.on('exit', (code, signal) => {
      this.running = false;
      this._stopPoll();
      this.emit('log', { line: `server exited (code=${code} signal=${signal || '-'})`, level: 'warn' });
      this.emit('state', this.info());
      this.emit('exit', { code, signal });
      this.child = null;
    });
    this.child.on('error', (e) => {
      this.emit('log', { line: `failed to launch: ${e.message}`, level: 'error' });
    });

    this._startPoll();
  }

  stop() {
    this._stopPoll();
    if (this.child && this.running) {
      const child = this.child;
      try { child.kill('SIGTERM'); } catch (_) {}
      setTimeout(() => { try { if (!child.killed) child.kill('SIGKILL'); } catch (_) {} }, 3000);
    }
  }

  // ── /status polling (localhost bypass, self-signed cert accepted) ────────
  _startPoll() {
    this._stopPoll();
    const port = 8766;
    const tick = () => {
      const req = (this.tls ? https : require('http')).get({
        host: '127.0.0.1', port, path: '/status', rejectUnauthorized: false, timeout: 2000,
      }, (res) => {
        let body = '';
        res.on('data', (d) => (body += d));
        res.on('end', () => {
          try {
            const s = JSON.parse(body);
            if (s.active !== this._lastActive) { this._lastActive = s.active; }
            this.emit('status', s);
          } catch (_) {}
        });
      });
      req.on('error', () => {});
      req.on('timeout', () => req.destroy());
    };
    this._poll = setInterval(tick, 1500);
  }

  _stopPoll() { if (this._poll) { clearInterval(this._poll); this._poll = null; } }

  // Ensure a user-writable venv for the packaged app — fully ASYNC (spawn, not
  // execFileSync) so the main thread never freezes. Resolves to the python
  // path, or null on failure.
  _ensureVenv() {
    const dir = path.join(app.getPath('userData'), 'venv');
    const py = path.join(dir, 'bin', 'python3');
    if (fs.existsSync(py)) return Promise.resolve(py);

    const run = (bin, a) => new Promise((res, rej) => {
      const p = spawn(bin, a, { stdio: 'ignore' });
      p.on('error', rej);
      p.on('exit', (code) => (code === 0 ? res() : rej(new Error(`${path.basename(bin)} exited ${code}`))));
    });

    this.emit('log', { line: 'First run: setting up the Python environment (one-time, ~20s)…', level: 'info' });
    this.emit('state', this.info());   // lets the UI show a "preparing" state
    return run('python3', ['-m', 'venv', dir])
      .then(() => { this.emit('log', { line: 'Installing dependencies…', level: 'info' }); return run(py, ['-m', 'pip', 'install', '--quiet', 'websockets', 'qrcode', 'zeroconf']); })
      .then(() => { this.emit('log', { line: 'Python environment ready.', level: 'info' }); return py; })
      .catch((e) => { this.emit('log', { line: `venv setup failed: ${e.message}`, level: 'error' }); return null; });
  }
}

module.exports = { ServerController, genPin };
