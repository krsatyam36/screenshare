#!/usr/bin/env python3
"""
screen-stream — Stream laptop + external monitor to a tablet browser over local WiFi.

Screens (from xrandr):
  laptop   → eDP   (AMD GPU)   1920x1080 @ +0,+1080
  external → HDMI  (RTX 3050)  1920x1080 @ +216,+0

Usage:  screen-share-tab   (or ./start.sh)
Tablet: http://screen-stream.local:8766  (or http://<ip>:8766)
"""

import asyncio
import atexit
import hashlib
import hmac
import http.server
import json
import logging
import os
import secrets
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ─── Dependency check ────────────────────────────────────────────────────────
try:
    import websockets
    import websockets.exceptions
except ImportError:
    print("ERROR: websockets not installed.\nRun: pip3 install websockets", file=sys.stderr)
    sys.exit(1)

# ─── Configuration ────────────────────────────────────────────────────────────
DISPLAY_ENV = os.environ.get('DISPLAY', ':0')
WS_PORT     = 8765
HTTP_PORT   = 8766
FPS         = 30
BITRATE     = '1500k'

SCREENS = {
    'laptop':   {'size': '1920x1080', 'offset': '0,1080'},
    'external': {'size': '1920x1080', 'offset': '0,0'},
}

# Global display bounds — used for cursor coordinate mapping
SCREEN_BOUNDS = {
    'laptop':   {'x': 0, 'y': 1080, 'w': 1920, 'h': 1080},
    'external': {'x': 0, 'y': 0,    'w': 1920, 'h': 1080},
}

SCRIPT_DIR = Path(__file__).parent
CONFIG_DIR = Path(os.path.expanduser('~/.config/screen-stream'))

# ─── Auth / TLS (set in main() from CLI/env) ───────────────────────────────────
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
    try:
        subprocess.run(
            ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
             '-keyout', KEY_PATH, '-out', CERT_PATH, '-days', '3650',
             '-subj', '/CN=screen-stream.local'],
            check=True, capture_output=True, timeout=30,
        )
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
 <input id=pin type=tel inputmode=numeric autocomplete=off autofocus placeholder="••••">
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def find_xauthority() -> str:
    uid = os.getuid()
    candidates = [
        os.environ.get('XAUTHORITY', ''),
        os.path.expanduser('~/.Xauthority'),
        f'/run/user/{uid}/gdm/Xauthority',
        f'/run/user/{uid}/.mutter-Xwaylandauth',
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return ''


def get_ws_path(websocket) -> str:
    try:
        return websocket.request.path
    except AttributeError:
        return getattr(websocket, 'path', '/')


def _ffmpeg_has_encoder(name: str) -> bool:
    r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                       capture_output=True, text=True)
    return f' {name} ' in r.stdout


def _detect_vaapi_device() -> str:
    import glob
    for node in sorted(glob.glob('/dev/dri/renderD*')):
        try:
            r = subprocess.run(
                ['vainfo', '--display', 'drm', '--device', node],
                capture_output=True, text=True, timeout=3,
            )
            if 'AMD' in r.stdout or 'radeon' in r.stdout.lower() or 'H.264' in r.stdout:
                return node
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    import glob as _g
    nodes = sorted(_g.glob('/dev/dri/renderD*'))
    return nodes[0] if nodes else ''


HW: dict = {}

# Resolved PulseAudio/PipeWire monitor source for the default sink ('' if none)
AUDIO_SRC: str = ''


def detect_audio_source() -> None:
    """Resolve the default sink's monitor source so we can capture playback.

    Works on PipeWire (via pipewire-pulse) and plain PulseAudio. The monitor
    source name is the sink's node.name with a '.monitor' suffix.
    """
    global AUDIO_SRC
    try:
        r = subprocess.run(
            ['wpctl', 'inspect', '@DEFAULT_AUDIO_SINK@'],
            capture_output=True, text=True, timeout=3, env=_x_env(),
        )
        for line in r.stdout.splitlines():
            if 'node.name' in line and '=' in line:
                name = line.split('=', 1)[1].strip().strip('"')
                if name:
                    AUDIO_SRC = f'{name}.monitor'
                    break
    except Exception:
        AUDIO_SRC = ''
    log.info(f"Audio — capture source: {AUDIO_SRC or 'none (no wpctl/sink)'}")


def detect_hw_encoders() -> None:
    global HW
    has_nvenc = _ffmpeg_has_encoder('h264_nvenc')
    has_vaapi = _ffmpeg_has_encoder('h264_vaapi')
    vaapi_dev = _detect_vaapi_device() if has_vaapi else ''
    HW['nvenc']     = has_nvenc
    HW['vaapi']     = has_vaapi and bool(vaapi_dev)
    HW['vaapi_dev'] = vaapi_dev
    log.info(f"Encoders — NVENC: {'yes' if HW['nvenc'] else 'no'} | "
             f"VAAPI: {'yes (' + vaapi_dev + ')' if HW['vaapi'] else 'no'}")


def build_ffmpeg_cmd(screen_id: str, bitrate: str, fps: int,
                     crop: dict | None = None, audio: bool = False) -> list[str]:
    s        = SCREENS[screen_id]
    keyframe = fps  # 1 keyframe/sec → fast reconnect sync
    src_w, src_h = map(int, s['size'].split('x'))
    want_audio = audio and bool(AUDIO_SRC)

    input_args = [
        'ffmpeg', '-loglevel', 'warning',
        '-fflags', 'nobuffer',
        '-thread_queue_size', '512',
        '-f', 'x11grab',
        '-r', str(fps),
        '-video_size', s['size'],
        '-i', f"{DISPLAY_ENV}+{s['offset']}",
    ]

    # Build crop+scale filter chain when focus mode is active
    crop_vf: list[str] = []
    if crop:
        crop_vf = [
            f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}",
            f"scale={src_w}:{src_h}",
        ]

    if screen_id == 'external' and HW.get('nvenc'):
        encode_args = [
            '-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'll',
            '-zerolatency', '1', '-rc', 'cbr',
            '-b:v', bitrate, '-maxrate', bitrate, '-bufsize', '375k',
            '-g', str(keyframe),
        ]
        if crop_vf:
            encode_args = ['-vf', ','.join(crop_vf)] + encode_args
    elif screen_id == 'laptop' and HW.get('vaapi'):
        input_args = [
            'ffmpeg', '-loglevel', 'warning',
            '-fflags', 'nobuffer',
            '-vaapi_device', HW['vaapi_dev'],
            '-thread_queue_size', '512',
            '-f', 'x11grab', '-r', str(fps),
            '-video_size', s['size'],
            '-i', f"{DISPLAY_ENV}+{s['offset']}",
        ]
        vaapi_vf = crop_vf + ['format=nv12', 'hwupload']
        encode_args = [
            '-vf', ','.join(vaapi_vf),
            '-c:v', 'h264_vaapi',
            '-b:v', bitrate, '-g', str(keyframe),
        ]
    else:
        encode_args = [
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-b:v', bitrate, '-maxrate', bitrate, '-bufsize', '375k',
            '-pix_fmt', 'yuv420p', '-g', str(keyframe),
        ]
        if crop_vf:
            encode_args = ['-vf', ','.join(crop_vf)] + encode_args

    if want_audio:
        # Capture the default-sink monitor and mux AAC into the same MPEG-TS
        # so mpegts.js plays it natively (no second socket / JS decoder).
        input_args = input_args + [
            '-f', 'pulse', '-thread_queue_size', '512', '-i', AUDIO_SRC,
        ]
        encode_args = encode_args + ['-c:a', 'aac', '-b:a', '128k', '-ac', '2']

    return input_args + encode_args + ['-f', 'mpegts', 'pipe:1']


# ─── mDNS (screen-stream.local) ──────────────────────────────────────────────

def start_mdns(ip: str, port: int) -> None:
    try:
        from zeroconf import ServiceInfo, Zeroconf
        info = ServiceInfo(
            "_http._tcp.local.",
            "screen-stream._http._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={},
            server="screen-stream.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        atexit.register(lambda: (zc.unregister_service(info), zc.close()))
        log.info("mDNS: screen-stream.local registered")
    except ImportError:
        log.warning("mDNS: zeroconf not installed — install with: pip install zeroconf")
    except Exception as exc:
        log.warning(f"mDNS: {exc}")


# ─── WebSocket stream handler ─────────────────────────────────────────────────

async def stream_screen(websocket, screen_id: str, bitrate: str, fps: int,
                        crop: dict | None = None, audio: bool = False) -> None:
    env = os.environ.copy()
    env['DISPLAY'] = DISPLAY_ENV
    xa = find_xauthority()
    if xa:
        env['XAUTHORITY'] = xa

    cmd = build_ffmpeg_cmd(screen_id, bitrate, fps, crop, audio)
    focus_info = f" crop={crop['x']},{crop['y']},{crop['w']}x{crop['h']}" if crop else ""
    audio_info = " +audio" if (audio and AUDIO_SRC) else ""
    log.info(f"[{screen_id}] {websocket.remote_address} — {bitrate} {fps}fps{focus_info}{audio_info}")

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                log.warning(f"[{screen_id}] ffmpeg exited — client will auto-reconnect")
                break
            await websocket.send(chunk)
    except (websockets.exceptions.ConnectionClosed,
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError):
        log.info(f"[{screen_id}] client disconnected")
    except Exception as exc:
        log.error(f"[{screen_id}] error: {exc}")
    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        log.info(f"[{screen_id}] stream closed")


def _ws_authed(websocket, params: dict) -> bool:
    if not AUTH_PIN:
        return True
    token = params.get('token', [''])[0]
    if not token:
        try:
            token = _cookie_token(websocket.request.headers.get('Cookie', ''))
        except Exception:
            token = ''
    return _check_token(token)


async def ws_handler(websocket) -> None:
    raw_path = get_ws_path(websocket)
    parsed   = urlparse(raw_path)
    params   = parse_qs(parsed.query)

    if not _ws_authed(websocket, params):
        await websocket.close(1008, 'authentication required')
        return

    route = parsed.path.strip('/').split('?')[0]
    if route == 'pty':
        if _PTY_OK:
            await pty_handler(websocket)
        else:
            await websocket.close(1011, 'pty unavailable')
        return

    screen_id = parsed.path.strip('/').removeprefix('screen/').split('?')[0]
    bitrate   = params.get('bitrate', [BITRATE])[0]
    fps       = max(1, min(60, int(params.get('fps', [str(FPS)])[0])))
    audio     = params.get('audio', ['0'])[0] in ('1', 'true', 'yes')

    crop = None
    if 'crop' in params:
        try:
            cx, cy, cw, ch = map(int, params['crop'][0].split(','))
            src_w, src_h   = map(int, SCREENS[screen_id]['size'].split('x'))
            cw = min(cw, src_w - max(0, cx))
            ch = min(ch, src_h - max(0, cy))
            cx, cy = max(0, cx), max(0, cy)
            if cw > 0 and ch > 0:
                crop = {'x': cx, 'y': cy, 'w': cw, 'h': ch}
        except (ValueError, KeyError):
            pass

    if screen_id not in SCREENS:
        log.warning(f"Unknown path: {raw_path!r}")
        await websocket.close(1008, f"Unknown screen '{screen_id}'")
        return

    await stream_screen(websocket, screen_id, bitrate, fps, crop, audio)


# ─── HTTP server ──────────────────────────────────────────────────────────────

# Detect helper tools once
def _have(tool: str) -> bool:
    return subprocess.run(['which', tool], capture_output=True).returncode == 0

_XDOTOOL    = _have('xdotool')
_XCLIP      = _have('xclip')
_GTK_LAUNCH = _have('gtk-launch')
_WPCTL      = _have('wpctl')
_LOGINCTL   = _have('loginctl')
_SYSTEMCTL  = _have('systemctl')


def _x_env() -> dict:
    env = os.environ.copy()
    env['DISPLAY'] = DISPLAY_ENV
    xa = find_xauthority()
    if xa:
        env['XAUTHORITY'] = xa
    return env


def _xdotool(*args: str, _input: str | None = None) -> None:
    if not _XDOTOOL:
        return
    try:
        subprocess.run(['xdotool', *args], env=_x_env(),
                       timeout=0.5, capture_output=True,
                       input=(_input.encode() if _input else None))
    except Exception as exc:
        log.warning(f"xdotool: {exc}")


# Whitelist for xdotool key/keychord — prevents shell injection
import re as _re
_KEY_RE = _re.compile(r'^[A-Za-z0-9_+\- ]{1,64}$')


def handle_input(cmd: dict) -> None:
    """Dispatch a remote-input JSON command to xdotool."""
    t      = cmd.get('type')
    screen = cmd.get('screen', 'laptop')
    b      = SCREEN_BOUNDS.get(screen)
    has_xy = 'x' in cmd and 'y' in cmd   # absent in relative/touchpad mode

    def _abs() -> tuple[str, str]:
        x = int(cmd.get('x', 0))
        y = int(cmd.get('y', 0))
        if b:
            x, y = b['x'] + x, b['y'] + y
        return str(x), str(y)

    def _moveto() -> list[str]:
        # In absolute mode, prefix a mousemove; in relative mode act in place.
        return ['mousemove', *_abs()] if has_xy else []

    try:
        if t == 'move':
            if has_xy:
                _xdotool('mousemove', *_abs())
        elif t == 'rmove':
            # Relative / touchpad-style cursor motion
            dx = int(cmd.get('dx', 0))
            dy = int(cmd.get('dy', 0))
            if dx or dy:
                _xdotool('mousemove_relative', '--', str(dx), str(dy))
        elif t == 'click':
            btn = str(int(cmd.get('button', 1)))
            _xdotool(*_moveto(), 'click', btn)
        elif t == 'down':
            btn = str(int(cmd.get('button', 1)))
            _xdotool(*_moveto(), 'mousedown', btn)
        elif t == 'up':
            btn = str(int(cmd.get('button', 1)))
            _xdotool(*_moveto(), 'mouseup', btn)
        elif t == 'scroll':
            dy = int(cmd.get('dy', 0))
            if dy == 0:
                return
            btn = '5' if dy > 0 else '4'   # X11: 4=up, 5=down
            n   = max(1, min(20, abs(dy)))
            _xdotool(*_moveto(),
                     'click', '--repeat', str(n), '--delay', '8', btn)
        elif t == 'key':
            keys = str(cmd.get('keys', ''))
            if _KEY_RE.match(keys):
                _xdotool('key', '--clearmodifiers', keys)
        elif t == 'type':
            text = str(cmd.get('text', ''))
            if text:
                # --delay 5ms gives reliable input without flooding
                _xdotool('type', '--delay', '5', '--', text)
    except Exception as exc:
        log.warning(f"input: {exc}")


def clipboard_get() -> str:
    if not _XCLIP:
        return ''
    try:
        r = subprocess.run(['xclip', '-selection', 'clipboard', '-o'],
                           env=_x_env(), capture_output=True,
                           text=True, timeout=1)
        return r.stdout
    except Exception:
        return ''


def clipboard_set(text: str) -> None:
    if not _XCLIP:
        return
    try:
        subprocess.run(['xclip', '-selection', 'clipboard', '-i'],
                       env=_x_env(), input=text.encode(), timeout=1)
    except Exception as exc:
        log.warning(f"xclip: {exc}")


# ─── App launcher + system controls ────────────────────────────────────────

_APPS_CACHE: list | None = None
_APP_ID_RE  = _re.compile(r'^[A-Za-z0-9._+-]{1,128}$')
_SINK       = '@DEFAULT_AUDIO_SINK@'


def _parse_desktop(path: str) -> dict | None:
    """Return {'name','icon'} for a launchable .desktop entry, else None."""
    name = icon = None
    in_entry = False
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.rstrip('\n')
                if line.startswith('['):
                    in_entry = (line.strip() == '[Desktop Entry]')
                    continue
                if not in_entry or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                key, val = key.strip(), val.strip()
                if key == 'Type' and val != 'Application':
                    return None
                if key in ('NoDisplay', 'Hidden') and val.lower() == 'true':
                    return None
                if key == 'Name' and name is None:
                    name = val
                elif key == 'Icon' and icon is None:
                    icon = val
    except Exception:
        return None
    return {'name': name, 'icon': icon or ''} if name else None


def list_desktop_apps() -> list:
    """Scan .desktop entries (cached). Later dirs override earlier ids."""
    global _APPS_CACHE
    if _APPS_CACHE is not None:
        return _APPS_CACHE
    import glob
    dirs = ['/usr/share/applications',
            os.path.expanduser('~/.local/share/applications')]
    seen: dict = {}
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, '*.desktop'))):
            app_id = os.path.basename(path)[:-len('.desktop')]
            entry  = _parse_desktop(path)
            if entry:
                seen[app_id] = entry
    apps = sorted(({'id': k, **v} for k, v in seen.items()),
                  key=lambda a: a['name'].lower())
    _APPS_CACHE = apps
    return apps


def launch_app(app_id: str) -> bool:
    if not _GTK_LAUNCH or not _APP_ID_RE.match(app_id or ''):
        return False
    try:
        subprocess.Popen(['gtk-launch', app_id], env=_x_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except Exception as exc:
        log.warning(f"launch: {exc}")
        return False


def system_action(action: str) -> bool:
    """Volume / media / power actions. Returns True if dispatched."""
    def run(cmd: list[str]) -> bool:
        subprocess.run(cmd, env=_x_env(), timeout=3, capture_output=True)
        return True
    try:
        if action == 'vol_up'   and _WPCTL:
            return run(['wpctl', 'set-volume', '-l', '1.5', _SINK, '5%+'])
        if action == 'vol_down' and _WPCTL:
            return run(['wpctl', 'set-volume', _SINK, '5%-'])
        if action == 'mute'     and _WPCTL:
            return run(['wpctl', 'set-mute', _SINK, 'toggle'])
        if action == 'lock'     and _LOGINCTL:
            return run(['loginctl', 'lock-session'])
        if action == 'suspend'  and _SYSTEMCTL:
            return run(['systemctl', 'suspend'])
        if action in ('media_play', 'media_next', 'media_prev') and _XDOTOOL:
            keymap = {'media_play': 'XF86AudioPlay',
                      'media_next': 'XF86AudioNext',
                      'media_prev': 'XF86AudioPrev'}
            _xdotool('key', keymap[action])
            return True
    except Exception as exc:
        log.warning(f"system {action}: {exc}")
    return False


# ─── File browser (jailed to $HOME) ─────────────────────────────────────────

HOME_DIR = os.path.realpath(os.path.expanduser('~'))


def _safe_path(rel: str) -> str | None:
    """Resolve a client path (relative to HOME, or absolute) inside HOME."""
    rel = rel or ''
    base = rel if rel.startswith('/') else os.path.join(HOME_DIR, rel)
    cand = os.path.realpath(base)
    if cand == HOME_DIR or cand.startswith(HOME_DIR + os.sep):
        return cand
    return None


def _rel(path: str) -> str:
    r = os.path.relpath(path, HOME_DIR)
    return '' if r == '.' else r


def list_dir(rel: str) -> dict:
    path = _safe_path(rel)
    if not path or not os.path.isdir(path):
        return {'error': 'not found'}
    entries = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            try:
                is_dir = os.path.isdir(full)
                st     = os.stat(full)
                entries.append({'name': name, 'dir': is_dir,
                                'size': 0 if is_dir else st.st_size,
                                'mtime': int(st.st_mtime)})
            except OSError:
                continue
    except OSError:
        return {'error': 'cannot read'}
    entries.sort(key=lambda e: (not e['dir'], e['name'].lower()))
    parent = None if path == HOME_DIR else _rel(os.path.dirname(path))
    return {'path': _rel(path), 'parent': parent, 'entries': entries}


def file_op(op: str, rel: str, arg: str = '') -> bool:
    import shutil
    path = _safe_path(rel)
    if not path:
        return False
    try:
        if op == 'mkdir':
            arg = os.path.basename(arg)
            if not arg:
                return False
            target = _safe_path(os.path.join(_rel(path), arg))
            if target:
                os.makedirs(target, exist_ok=True)
                return True
        elif op == 'rename':
            arg = os.path.basename(arg)
            if not arg or path == HOME_DIR:
                return False
            target = _safe_path(os.path.join(_rel(os.path.dirname(path)), arg))
            if target:
                os.rename(path, target)
                return True
        elif op == 'delete':
            if path == HOME_DIR:
                return False
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return True
    except Exception as exc:
        log.warning(f"fileop {op}: {exc}")
    return False


# ─── Battery ────────────────────────────────────────────────────────────────

def _battery_info() -> dict:
    import glob
    for base in sorted(glob.glob('/sys/class/power_supply/BAT*')):
        try:
            with open(os.path.join(base, 'capacity')) as f:
                cap = int(f.read().strip())
            status = 'Unknown'
            try:
                with open(os.path.join(base, 'status')) as f:
                    status = f.read().strip()
            except OSError:
                pass
            return {'capacity': cap, 'status': status}
        except Exception:
            continue
    return {}


# ─── Web terminal (PTY over WebSocket) ──────────────────────────────────────

_PTY_OK = hasattr(os, 'fork')


async def pty_handler(websocket) -> None:
    import pty, fcntl, termios, struct, signal
    pid, fd = pty.fork()
    if pid == 0:                              # child → exec the shell
        shell = os.environ.get('SHELL', '/bin/bash')
        env = os.environ.copy()
        env['TERM'] = 'xterm-256color'
        try:
            os.execvpe(shell, [shell], env)
        except Exception:
            os._exit(1)

    log.info(f"[pty] shell started (pid {pid})")
    loop = asyncio.get_running_loop()

    def _on_fd_readable():
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b''
        if not data:
            loop.remove_reader(fd)
            asyncio.ensure_future(websocket.close())
            return
        asyncio.ensure_future(websocket.send(data.decode('utf-8', 'replace')))

    loop.add_reader(fd, _on_fd_readable)
    try:
        async for msg in websocket:
            try:
                obj = json.loads(msg)
            except Exception:
                continue
            if 'i' in obj:
                os.write(fd, str(obj['i']).encode('utf-8'))
            elif 'r' in obj and isinstance(obj['r'], list) and len(obj['r']) == 2:
                cols, rows = int(obj['r'][0]), int(obj['r'][1])
                fcntl.ioctl(fd, termios.TIOCSWINSZ,
                            struct.pack('HHHH', rows, cols, 0, 0))
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as exc:
        log.warning(f"[pty] {exc}")
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except Exception:
            pass
        log.info("[pty] closed")


def run_http_server() -> None:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

        # ── Auth helpers ────────────────────────────────────────────────
        def _is_authed(self) -> bool:
            return _check_token(_cookie_token(self.headers.get('Cookie', '')))

        def _deny(self, code: int = 401) -> None:
            self.send_response(code)
            self.send_header('Content-Length', '0')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

        def do_GET(self):
            # ── Auth gate ────────────────────────────────────────────────
            if AUTH_PIN and not self._is_authed():
                if self.path in ('/', '/index.html'):
                    page = LOGIN_PAGE.encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                else:
                    self._deny()
                return

            # ── /ping — RTT probe ────────────────────────────────────────
            if self.path == '/ping':
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            # ── /cursor — live cursor position for highlight overlay ─────
            if self.path == '/cursor' and _XDOTOOL:
                try:
                    r = subprocess.run(
                        ['xdotool', 'getmouselocation'],
                        capture_output=True, text=True, timeout=0.05,
                        env={**os.environ, 'DISPLAY': DISPLAY_ENV},
                    )
                    # Output: "x:1234 y:567 screen:0 window:..."
                    parts = dict(p.split(':') for p in r.stdout.strip().split()
                                 if ':' in p)
                    cx = int(parts.get('x', -1))
                    cy = int(parts.get('y', -1))

                    # Map global coords to per-screen coords + which screen
                    result = {'x': -1, 'y': -1, 'screen': None}
                    for sid, b in SCREEN_BOUNDS.items():
                        if b['x'] <= cx < b['x'] + b['w'] and \
                           b['y'] <= cy < b['y'] + b['h']:
                            result = {
                                'x': cx - b['x'],
                                'y': cy - b['y'],
                                'screen': sid,
                                'sw': b['w'],
                                'sh': b['h'],
                            }
                            break

                    data = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_response(503)
                    self.end_headers()
                return

            # ── /capabilities — tell client what server supports ─────────
            if self.path == '/capabilities':
                data = json.dumps({
                    'cursor':    _XDOTOOL,
                    'control':   _XDOTOOL,
                    'clipboard': _XCLIP,
                    'audio':     bool(AUDIO_SRC),
                    'apps':      _GTK_LAUNCH,
                    'system':    _WPCTL or _LOGINCTL or _SYSTEMCTL or _XDOTOOL,
                    'files':     True,
                    'terminal':  _PTY_OK,
                }).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
                return

            # ── /battery — laptop battery level for the top bar ──────────
            if self.path == '/battery':
                data = json.dumps(_battery_info()).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
                return

            # ── /apps — installed .desktop launchers ─────────────────────
            if self.path == '/apps':
                data = json.dumps({'apps': list_desktop_apps()}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
                return

            # ── /clipboard GET — read laptop clipboard ───────────────────
            if self.path == '/clipboard':
                text = clipboard_get()
                data = json.dumps({'text': text}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
                return

            parsed = urlparse(self.path)

            # ── /files — list a directory (jailed to $HOME) ──────────────
            if parsed.path == '/files':
                rel  = parse_qs(parsed.query).get('path', [''])[0]
                data = json.dumps(list_dir(rel)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
                return

            # ── /download — stream a file to the tablet ──────────────────
            if parsed.path == '/download':
                rel  = parse_qs(parsed.query).get('path', [''])[0]
                path = _safe_path(rel)
                if not path or not os.path.isfile(path):
                    self._deny(404)
                    return
                try:
                    size = os.path.getsize(path)
                    fname = os.path.basename(path).replace('"', '')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(size))
                    self.send_header('Content-Disposition',
                                     f'attachment; filename="{fname}"')
                    self.end_headers()
                    with open(path, 'rb') as f:
                        while True:
                            chunk = f.read(262144)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except Exception:
                    pass
                return

            super().do_GET()

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length) if length else b''

            # ── /auth — exchange PIN for a session cookie ────────────────
            if self.path == '/auth':
                ip = self.client_address[0]
                if _auth_rate_limited(ip):
                    self._deny(429)
                    return
                try:
                    data = json.loads(body or b'{}')
                    pin  = str(data.get('pin', ''))
                except Exception:
                    pin = ''
                if AUTH_PIN and hmac.compare_digest(pin, AUTH_PIN):
                    self.send_response(204)
                    self.send_header(
                        'Set-Cookie',
                        f'ss_auth={_AUTH_TOKEN}; Path=/; Max-Age=86400; SameSite=Lax',
                    )
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                else:
                    _auth_record_fail(ip)
                    self._deny(401)
                return

            # ── Auth gate for everything else ────────────────────────────
            if AUTH_PIN and not self._is_authed():
                self._deny()
                return

            # ── /input — remote control commands ─────────────────────────
            if self.path == '/input':
                try:
                    cmd = json.loads(body or b'{}')
                    handle_input(cmd)
                    self.send_response(204)
                except Exception:
                    self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            # ── /clipboard POST — set laptop clipboard ──────────────────
            if self.path == '/clipboard':
                try:
                    data = json.loads(body or b'{}')
                    clipboard_set(str(data.get('text', '')))
                    self.send_response(204)
                except Exception:
                    self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            # ── /launch — start a .desktop app ───────────────────────────
            if self.path == '/launch':
                try:
                    data = json.loads(body or b'{}')
                    ok = launch_app(str(data.get('id', '')))
                    self.send_response(204 if ok else 400)
                except Exception:
                    self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            # ── /system — volume / media / power controls ────────────────
            if self.path == '/system':
                try:
                    data = json.loads(body or b'{}')
                    ok = system_action(str(data.get('action', '')))
                    self.send_response(204 if ok else 400)
                except Exception:
                    self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            parsed = urlparse(self.path)

            # ── /upload?path=<dir>&name=<file> — raw body is the file ────
            if parsed.path == '/upload':
                q    = parse_qs(parsed.query)
                rel  = q.get('path', [''])[0]
                name = os.path.basename(q.get('name', [''])[0])
                d    = _safe_path(rel)
                ok   = False
                if d and os.path.isdir(d) and name:
                    dest = _safe_path(os.path.join(_rel(d), name))
                    if dest:
                        try:
                            with open(dest, 'wb') as f:
                                f.write(body)
                            ok = True
                        except Exception as exc:
                            log.warning(f"upload: {exc}")
                self.send_response(204 if ok else 400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            # ── /fileop — mkdir / rename / delete ────────────────────────
            if parsed.path == '/fileop':
                try:
                    data = json.loads(body or b'{}')
                    ok = file_op(str(data.get('op', '')),
                                 str(data.get('path', '')),
                                 str(data.get('arg', '')))
                    self.send_response(204 if ok else 400)
                except Exception:
                    self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, *_):
            pass

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    with ReusableTCPServer(('', HTTP_PORT), Handler) as httpd:
        ctx = make_ssl_context()
        if ctx:
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        httpd.serve_forever()


# ─── Entry point ──────────────────────────────────────────────────────────────

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


async def main() -> None:
    ip = get_local_ip()
    detect_hw_encoders()
    detect_audio_source()
    start_mdns(ip, HTTP_PORT)

    ssl_ctx = make_ssl_context()      # also generates the cert if needed
    scheme    = 'https' if (USE_TLS and ssl_ctx) else 'http'
    ws_scheme = 'wss'   if (USE_TLS and ssl_ctx) else 'ws'

    enc_laptop   = 'h264_vaapi' if HW.get('vaapi') else 'libx264'
    enc_external = 'h264_nvenc' if HW.get('nvenc') else 'libx264'

    threading.Thread(target=run_http_server, daemon=True).start()
    log.info(f"HTTP → port {HTTP_PORT}")

    url = f"{scheme}://{ip}:{HTTP_PORT}"
    bar = '─' * 54
    print(f"\n{bar}")
    print(f"  Screen Stream — ready")
    print(f"{bar}")
    print(f"  Tablet URL  →  {url}")
    print(f"  mDNS URL    →  {scheme}://screen-stream.local:{HTTP_PORT}")
    print(f"  WebSocket   →  {ws_scheme}://{ip}:{WS_PORT}")
    print(f"  Laptop      →  eDP   1920x1080  [{enc_laptop}]")
    print(f"  External    →  HDMI  1920x1080  [{enc_external}]")
    print(f"  Audio       →  {'on (' + AUDIO_SRC + ')' if AUDIO_SRC else 'no source (PipeWire/Pulse + wpctl needed)'}")
    print(f"  Auth        →  {'PIN ' + AUTH_PIN + '   · enter this on the tablet' if AUTH_PIN else 'open (trusted LAN)'}")
    print(f"  Encryption  →  {'TLS on (self-signed — accept the cert)' if (USE_TLS and ssl_ctx) else 'off (plaintext)'}")
    print(f"  xdotool     →  {'available (cursor highlight on)' if _XDOTOOL else 'not found (cursor highlight off)'}")
    print(f"{bar}\n")
    if USE_TLS and ssl_ctx:
        print(f"  TLS note: first visit may warn about the self-signed cert on")
        print(f"  BOTH {scheme}://{ip}:{HTTP_PORT} and {ws_scheme}://{ip}:{WS_PORT} — accept both.\n")

    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print(f"\n  ^ Scan to open on tablet\n")
    except ImportError:
        pass

    print("  Ctrl+C to stop\n")

    async with websockets.serve(
        ws_handler,
        host='0.0.0.0',
        port=WS_PORT,
        ping_interval=15,
        ping_timeout=10,
        max_size=None,
        ssl=ssl_ctx,
    ):
        await asyncio.Future()


if __name__ == '__main__':
    parse_args(sys.argv[1:])
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nShutting down. Bye!')
