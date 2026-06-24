// Small JSON settings store at ~/.config/screen-stream/app-settings.json
const os = require('os');
const path = require('path');
const fs = require('fs');

const DIR = path.join(os.homedir(), '.config', 'screen-stream');
const FILE = path.join(DIR, 'app-settings.json');

const DEFAULTS = {
  pinMode: 'rotating',   // 'rotating' | 'fixed'
  fixedPin: '',
  tls: true,
  autostart: false,
};

function read() {
  try { return { ...DEFAULTS, ...JSON.parse(fs.readFileSync(FILE, 'utf8')) }; }
  catch (_) { return { ...DEFAULTS }; }
}

function write(patch) {
  const next = { ...read(), ...patch };
  fs.mkdirSync(DIR, { recursive: true });
  fs.writeFileSync(FILE, JSON.stringify(next, null, 2));
  return next;
}

module.exports = { read, write, DEFAULTS, FILE };
