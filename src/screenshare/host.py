"""Host-side capabilities: networking, X env, input, clipboard, app launcher,
system controls and battery."""
import glob
import os
import re as _re
import socket
import subprocess

from .config import DISPLAY_ENV, SCREEN_BOUNDS, log

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




# ─── App icon resolution (freedesktop lookup) ───────────────────────────────

_ICON_CT = {'.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg'}


def find_app_icon(app_id: str) -> tuple[str, str] | None:
    """Resolve a .desktop app's Icon= to an actual file. Returns (path, ctype)
    for a browser-renderable image (svg/png/jpg), else None."""
    if not _APP_ID_RE.match(app_id or ''):
        return None
    icon = ''
    for a in list_desktop_apps():
        if a['id'] == app_id:
            icon = a.get('icon', '')
            break
    if not icon:
        return None

    def _ct(p: str):
        ext = os.path.splitext(p)[1].lower()
        return (p, _ICON_CT[ext]) if ext in _ICON_CT else None

    if os.path.isabs(icon):
        return _ct(icon) if os.path.exists(icon) else None

    bases = [os.path.expanduser('~/.local/share/icons'),
             '/usr/share/icons', '/usr/local/share/icons']
    pats = []
    for b in bases:
        pats += [f'{b}/*/scalable/apps/{icon}.svg',
                 f'{b}/*/256x256/apps/{icon}.png',
                 f'{b}/*/128x128/apps/{icon}.png',
                 f'{b}/*/96x96/apps/{icon}.png',
                 f'{b}/*/64x64/apps/{icon}.png',
                 f'{b}/*/48x48/apps/{icon}.png']
    pats += [f'/usr/share/pixmaps/{icon}.svg',
             f'/usr/share/pixmaps/{icon}.png']
    for pat in pats:
        m = sorted(glob.glob(pat))
        if m:
            return _ct(m[0])
    return None
