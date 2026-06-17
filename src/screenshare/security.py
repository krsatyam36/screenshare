"""PIN authentication, TLS, and CLI/env argument parsing."""
import hashlib
import hmac
import os
import secrets
import ssl
import subprocess
import time

from .config import CONFIG_DIR, log

AUTH_PIN: str | None = None              # None → auth disabled (trusted-LAN mode)
USE_TLS:  bool       = False
CERT_PATH: str       = ''
KEY_PATH:  str       = ''

# Per-process auth token derived from a random secret. Restarting the server
# invalidates outstanding sessions, which is fine for a personal LAN tool.
_AUTH_SECRET = secrets.token_bytes(32)
_AUTH_TOKEN  = hmac.new(_AUTH_SECRET, b'screen-stream-v1', hashlib.sha256).hexdigest()
_AUTH_FAILS: dict = {}                    # ip → [count, window_start_ts]
_AUTH_MAX_FAILS = 8
_AUTH_WINDOW    = 60.0


def _check_token(token: str) -> bool:
    """True if auth is disabled or the token matches (constant-time)."""
    if not AUTH_PIN:
        return True
    return bool(token) and hmac.compare_digest(token, _AUTH_TOKEN)


def _cookie_token(cookie_header: str) -> str:
    for part in (cookie_header or '').split(';'):
        if '=' in part:
            k, v = part.strip().split('=', 1)
            if k == 'ss_auth':
                return v
    return ''


def _auth_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    cnt, start = _AUTH_FAILS.get(ip, [0, now])
    if now - start > _AUTH_WINDOW:
        cnt, start = 0, now
    _AUTH_FAILS[ip] = [cnt, start]
    return cnt >= _AUTH_MAX_FAILS


def _auth_record_fail(ip: str) -> None:
    now = time.monotonic()
    cnt, start = _AUTH_FAILS.get(ip, [0, now])
    if now - start > _AUTH_WINDOW:
        cnt, start = 0, now
    _AUTH_FAILS[ip] = [cnt + 1, start]


def _ensure_cert() -> bool:
    """Generate a self-signed cert/key in CONFIG_DIR if absent. Returns ok."""
    global CERT_PATH, KEY_PATH
    if not CERT_PATH:
        CERT_PATH = str(CONFIG_DIR / 'cert.pem')
    if not KEY_PATH:
        KEY_PATH = str(CONFIG_DIR / 'key.pem')
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        return True
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # subjectAltName covers the LAN IP + hostnames so the cert is valid-for-host
    # (avoids name-mismatch errors; the self-signed root still needs accepting).
    try:
        from .host import get_local_ip
        ip = get_local_ip()
    except Exception:
        ip = '127.0.0.1'
    san = f"subjectAltName=DNS:screen-stream.local,DNS:localhost,IP:{ip},IP:127.0.0.1"

    base = ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', KEY_PATH, '-out', CERT_PATH, '-days', '3650',
            '-subj', '/CN=screen-stream.local']
    try:
        try:
            subprocess.run(base + ['-addext', san],
                           check=True, capture_output=True, timeout=30)
        except subprocess.CalledProcessError:
            # Older openssl without -addext → fall back to a plain cert
            subprocess.run(base, check=True, capture_output=True, timeout=30)
        os.chmod(KEY_PATH, 0o600)
        log.info(f"TLS: generated self-signed cert at {CERT_PATH}")
        return True
    except Exception as exc:
        log.error(f"TLS: could not generate cert ({exc}) — falling back to plaintext")
        return False


def make_ssl_context() -> ssl.SSLContext | None:
    if not USE_TLS or not _ensure_cert():
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    return ctx


LOGIN_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Screen Stream — Login</title>
<style>
 body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
  background:#0a0a0a;color:#ddd;font-family:system-ui,sans-serif}
 .box{background:#161616;border:1px solid #2a2a2a;border-radius:14px;padding:28px;
  width:280px;text-align:center}
 h1{font-size:18px;margin:0 0 4px}p{color:#888;font-size:13px;margin:0 0 18px}
 input{width:100%;box-sizing:border-box;background:#0d0d0d;border:1px solid #2a2a2a;
  border-radius:8px;color:#ddd;padding:12px;font-size:18px;text-align:center;letter-spacing:4px}
 button{margin-top:14px;width:100%;padding:12px;border:0;border-radius:8px;
  background:#2563eb;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
 .err{color:#f87171;font-size:13px;min-height:18px;margin-top:10px}
</style></head><body>
<div class=box>
 <h1>🖥️ Screen Stream</h1><p>Enter the PIN to continue</p>
 <input id=pin type=text inputmode=text autocomplete=off autocorrect=off autocapitalize=none spellcheck=false autofocus placeholder="PIN">
 <button onclick=go()>Unlock</button>
 <div class=err id=err></div>
</div>
<script>
 const i=document.getElementById('pin'),e=document.getElementById('err');
 i.addEventListener('keydown',ev=>{if(ev.key==='Enter')go()});
 async function go(){
   e.textContent='';
   try{
     const r=await fetch('/auth',{method:'POST',body:JSON.stringify({pin:i.value})});
     if(r.ok){location.reload();}
     else if(r.status===429){e.textContent='Too many attempts — wait a minute.';}
     else{e.textContent='Wrong PIN.';i.value='';i.focus();}
   }catch(_){e.textContent='Connection error.';}
 }
</script></body></html>"""



def parse_args(argv: list[str]) -> None:
    """Configure auth/TLS globals from CLI flags and environment.

    Flags:  --pin <PIN> | --no-pin   --tls | --no-tls   --cert <p> --key <p>
    Env:    SCREENSHARE_PIN
    Default (no flags): no auth, no TLS — trusted-LAN mode, unchanged.
    """
    global AUTH_PIN, USE_TLS, CERT_PATH, KEY_PATH
    AUTH_PIN = os.environ.get('SCREENSHARE_PIN') or None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--pin' and i + 1 < len(argv):
            AUTH_PIN = argv[i + 1]; i += 2; continue
        if a == '--no-pin':
            AUTH_PIN = None; i += 1; continue
        if a == '--tls':
            USE_TLS = True; i += 1; continue
        if a == '--no-tls':
            USE_TLS = False; i += 1; continue
        if a == '--cert' and i + 1 < len(argv):
            CERT_PATH = argv[i + 1]; i += 2; continue
        if a == '--key' and i + 1 < len(argv):
            KEY_PATH = argv[i + 1]; i += 2; continue
        i += 1
    if AUTH_PIN is not None and not AUTH_PIN:
        AUTH_PIN = None

