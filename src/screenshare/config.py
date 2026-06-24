"""Shared configuration, paths and logging for Screen Stream."""
import logging
import os
import sys
from pathlib import Path

# Friendly dependency check — cached for every module that imports websockets.
try:
    import websockets        # noqa: F401
    import websockets.exceptions  # noqa: F401
except ImportError:
    print("ERROR: websockets not installed.\nRun: pip3 install websockets", file=sys.stderr)
    sys.exit(1)

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

PACKAGE_DIR = Path(__file__).parent
WEB_DIR     = PACKAGE_DIR / 'web'
CONFIG_DIR  = Path(os.path.expanduser('~/.config/screen-stream'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('screenshare')

# Live runtime stats — read by the /status endpoint (consumed by the desktop
# app to show connection state). Mutated in place by the WebSocket handler.
STATS = {'active': 0, 'started': None, 'clients': []}
