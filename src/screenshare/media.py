"""Video/audio capture: hardware-encoder detection, ffmpeg command building,
and the per-client streaming coroutine."""
import asyncio
import glob
import os
import subprocess

import websockets
import websockets.exceptions

from .config import SCREENS, DISPLAY_ENV, log
from .host import _x_env, find_xauthority

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

