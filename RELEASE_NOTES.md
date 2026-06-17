# Release v2.0.0: Product-Grade Refactor & Polish

This release hardens Screen Stream into a real product: a clean modular codebase, secure-by-default access, richer touch interaction, and a one-tap desktop app.

## ✨ What's New

- **Secure by default, rotating PIN:** every launch generates a fresh **alphanumeric** PIN (held in memory — never written to disk or committed) and serves over self-signed TLS whose certificate now includes the LAN IP/hostnames in its SAN. `--no-tls`/`--no-pin` remain local-debug escape hatches.
- **One command + desktop app:** run `screenshare` from anywhere, or install a launcher (`./install-app.sh`) with a custom logo that opens the server in a terminal.
- **Real app icons:** the launcher shows each app's actual icon (freedesktop lookup), with a letter-tile fallback.
- **Gestures, reworked:** pinch-to-zoom now works in *every* mode; two-finger drag pans the zoomed frame; two-finger up/down scrolls like a mouse wheel — on the stream and in the web terminal.
- **Modular codebase:** the monolithic `server.py` is split into a `src/screenshare/` package (`config`, `security`, `media`, `host`, `files`, `terminal`, `httpapp`, `wsapp`, `server`). Behaviour is unchanged and verified end-to-end.

## 🔧 Notes

- Code now lives under `src/`; run via `screenshare` / `./start.sh` (which launches `python -m screenshare`).
- No personal/third-party identifiers in the codebase; secrets are git-ignored.

---

# Release v1.1.0: Full Remote-Desktop Upgrade

This release turns Screen Stream from a "second monitor with basic control" into a tool you can use to run your laptop entirely from a tablet across the LAN — no physical access needed.

## ✨ What's New

- **Reliable mobile typing (the big fix):** keyboard capture was rewritten to use `input`/composition events instead of `keydown`. Android soft keyboards report `Unidentified`/keyCode 229 for printable characters, so the old path silently dropped everything you typed. Swipe-typing and autocorrect now work too, and the soft keyboard stays focused.
- **Touchpad / relative pointer mode:** drag to move the cursor like a laptop trackpad, for fine control — alongside the original absolute tap-to-position mode.
- **Audio forwarding:** laptop sound is captured from the PipeWire/PulseAudio monitor and muxed as AAC into the existing MPEG-TS stream, so `mpegts.js` plays it natively with no extra socket or decoder.
- **App launcher & system controls:** browse and launch `.desktop` apps (`gtk-launch`); volume/mute (`wpctl`), media keys (`xdotool`), lock (`loginctl`) and suspend (`systemctl`).
- **File transfer:** browse `$HOME` (jailed), download to the tablet, and upload from it.
- **Web terminal:** a real PTY behind an `xterm.js` terminal in the browser, with live resize.
- **Secure by default:** every launch requires a PIN (auto-generated on first run, shown in the banner) and serves over self-signed TLS (`https`/`wss`). One secured front door covers the whole app — web UI and both WebSockets — via an HMAC session cookie. `--no-tls`/`--no-pin` remain as local-debugging escape hatches.
- **One command:** run `screenshare` from any terminal; all features (screen, audio, control, touchpad, apps/system, files, terminal) come up together — no per-feature commands.
- **UX:** laptop battery indicator, auto-quality that adapts bitrate to RTT, and a landscape hint.

All new capabilities are feature-detected and degrade gracefully when the host tool isn't installed.

---

# Release v1.0.0: Initial Major Release

This is the first stable release of Screen Stream, establishing a zero-configuration, high-performance pipeline to turn any mobile device into a fully interactive second monitor for Linux.

## 🚀 Core Features & Contributions

- **Hardware-Accelerated Capture:** I implemented ffmpeg with x11grab and automatic hardware encoder selection (NVENC/VAAPI) to ensure minimal CPU overhead.

- **Low-Latency Streaming:** I engineered the pipeline to encode video as H.264 MPEG-TS, delivering it over WebSockets to be rendered natively in the browser via mpegts.js for sub-500ms latency.

- **Zero-Install Client:** No application is required on the receiving tablet or phone. The stream runs purely through a local URL on any modern web browser.

- **Interactive Remote Control:** I built custom touch-to-mouse coordinate mapping utilizing xdotool to seamlessly control the Linux host directly from the client screen.

- **Clipboard Synchronization:** Integrated two-way clipboard syncing between the host and the client device using xclip.

## 🛠️ Deployment & Packaging

- **Systemd Integration:** Created install-service.sh to seamlessly run the application as a persistent background service.

- **Debian Packaging:** Developed automated build scripts (packaging/build-deb.sh) for generating .deb installers, streamlining distribution.

## ⚠️ Known Limitations

- **Display Server Compatibility:** The current architecture relies on x11grab and xdotool, which are optimized for X11 environments. Wayland users may need to run via Xwayland for full functionality. Native Wayland protocols are planned for future major releases.
