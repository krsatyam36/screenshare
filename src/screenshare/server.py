"""Entry point: wires the modules together, prints the banner, and runs the
HTTP + WebSocket servers."""
import asyncio
import sys
import threading

import websockets

from .config import HTTP_PORT, WS_PORT, log
from . import security
from . import media
from .host import get_local_ip, _XDOTOOL
from .httpapp import run_http_server
from .wsapp import ws_handler, start_mdns


async def main() -> None:
    ip = get_local_ip()
    media.detect_hw_encoders()
    media.detect_audio_source()
    start_mdns(ip, HTTP_PORT)

    ssl_ctx = security.make_ssl_context()      # also generates the cert if needed
    scheme    = 'https' if (security.USE_TLS and ssl_ctx) else 'http'
    ws_scheme = 'wss'   if (security.USE_TLS and ssl_ctx) else 'ws'

    enc_laptop   = 'h264_vaapi' if media.HW.get('vaapi') else 'libx264'
    enc_external = 'h264_nvenc' if media.HW.get('nvenc') else 'libx264'

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
    print(f"  Audio       →  {'on (' + media.AUDIO_SRC + ')' if media.AUDIO_SRC else 'no source (PipeWire/Pulse + wpctl needed)'}")
    print(f"  Auth        →  {'PIN ' + security.AUTH_PIN + '   · enter this on the tablet' if security.AUTH_PIN else 'open (trusted LAN)'}")
    print(f"  Encryption  →  {'TLS on (self-signed — accept the cert)' if (security.USE_TLS and ssl_ctx) else 'off (plaintext)'}")
    print(f"  xdotool     →  {'available (cursor highlight on)' if _XDOTOOL else 'not found (cursor highlight off)'}")
    print(f"{bar}\n")
    if security.USE_TLS and ssl_ctx:
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


def run() -> None:
    security.parse_args(sys.argv[1:])
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nShutting down. Bye!')


if __name__ == '__main__':
    run()
