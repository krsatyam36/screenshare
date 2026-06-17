"""Web terminal: a PTY-backed shell bridged over a WebSocket."""
import asyncio
import json
import os

import websockets
import websockets.exceptions

from .config import log

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

