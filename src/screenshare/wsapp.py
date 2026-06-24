"""WebSocket entry point: routes /pty to the terminal and /<screen> to the
video stream, enforces auth, and registers mDNS."""
import asyncio
import atexit
import socket
import time
from urllib.parse import urlparse, parse_qs

import websockets
import websockets.exceptions

from .config import SCREENS, WS_PORT, BITRATE, FPS, STATS, log
from . import security
from .media import stream_screen
from .terminal import pty_handler, _PTY_OK

def get_ws_path(websocket) -> str:
    try:
        return websocket.request.path
    except AttributeError:
        return getattr(websocket, 'path', '/')



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
        log.info("mDNS: zeroconf not installed — skipping screen-stream.local (use the IP URL)")
    except Exception as exc:
        msg = str(exc).strip() or type(exc).__name__
        log.info(f"mDNS: screen-stream.local unavailable ({msg}) — use the IP URL instead")



def _ws_authed(websocket, params: dict) -> bool:
    if not security.AUTH_PIN:
        return True
    token = params.get('token', [''])[0]
    if not token:
        try:
            token = security._cookie_token(websocket.request.headers.get('Cookie', ''))
        except Exception:
            token = ''
    return security._check_token(token)


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

    client = {
        'ip': websocket.remote_address[0] if websocket.remote_address else '?',
        'screen': screen_id,
        'since': time.time(),
    }
    STATS['clients'].append(client)
    STATS['active'] = len(STATS['clients'])
    try:
        await stream_screen(websocket, screen_id, bitrate, fps, crop, audio)
    finally:
        try:
            STATS['clients'].remove(client)
        except ValueError:
            pass
        STATS['active'] = len(STATS['clients'])
