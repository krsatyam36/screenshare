"""HTTP server: static web app, auth/login, capabilities, cursor, clipboard,
input, apps/system, files, battery."""
import hmac
import http.server
import json
import os
import socketserver
import subprocess
from urllib.parse import urlparse, parse_qs

from .config import WEB_DIR, DISPLAY_ENV, SCREEN_BOUNDS, HTTP_PORT, STATS, log
from . import security
from . import media
from .host import (
    _XDOTOOL, _XCLIP, _GTK_LAUNCH, _WPCTL, _LOGINCTL, _SYSTEMCTL,
    handle_input, clipboard_get, clipboard_set,
    list_desktop_apps, launch_app, system_action, _battery_info,
    find_app_icon,
)
from .files import list_dir, file_op, _safe_path, _rel
from .terminal import _PTY_OK

def run_http_server() -> None:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_DIR), **kwargs)

        # ── Auth helpers ────────────────────────────────────────────────
        def _is_authed(self) -> bool:
            return security._check_token(security._cookie_token(self.headers.get('Cookie', '')))

        def _deny(self, code: int = 401) -> None:
            self.send_response(code)
            self.send_header('Content-Length', '0')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

        def do_GET(self):
            # ── /status — live state for the desktop app (localhost only) ─
            # Allowed without a PIN when requested from the machine itself,
            # so the local control-panel app can poll connection state.
            if urlparse(self.path).path == '/status':
                if self.client_address[0] not in ('127.0.0.1', '::1', 'localhost'):
                    self._deny()
                    return
                data = json.dumps({
                    'active':  STATS['active'],
                    'clients': STATS['clients'],
                    'started': STATS['started'],
                }).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
                return

            # ── Auth gate ────────────────────────────────────────────────
            if security.AUTH_PIN and not self._is_authed():
                if self.path in ('/', '/index.html'):
                    page = security.LOGIN_PAGE.encode()
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
                    'audio':     bool(media.AUDIO_SRC),
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

            # ── /appicon — the real icon for a .desktop app ──────────────
            if self.path.startswith('/appicon'):
                app_id = parse_qs(urlparse(self.path).query).get('id', [''])[0]
                hit = find_app_icon(app_id)
                if not hit:
                    self._deny(404)
                    return
                path, ctype = hit
                try:
                    with open(path, 'rb') as f:
                        blob = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', str(len(blob)))
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Cache-Control', 'max-age=86400')
                    self.end_headers()
                    self.wfile.write(blob)
                except Exception:
                    self._deny(404)
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
                if security._auth_rate_limited(ip):
                    self._deny(429)
                    return
                try:
                    data = json.loads(body or b'{}')
                    pin  = str(data.get('pin', ''))
                except Exception:
                    pin = ''
                if security.AUTH_PIN and hmac.compare_digest(pin, security.AUTH_PIN):
                    self.send_response(204)
                    self.send_header(
                        'Set-Cookie',
                        f'ss_auth={security._AUTH_TOKEN}; Path=/; Max-Age=86400; SameSite=Lax',
                    )
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                else:
                    security._auth_record_fail(ip)
                    self._deny(401)
                return

            # ── Auth gate for everything else ────────────────────────────
            if security.AUTH_PIN and not self._is_authed():
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
        ctx = security.make_ssl_context()
        if ctx:
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        httpd.serve_forever()

