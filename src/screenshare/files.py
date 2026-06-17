"""Home-jailed file browser operations."""
import os

from .config import log

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

