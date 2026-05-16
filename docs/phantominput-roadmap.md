# PhantomInput — input bridge for WebRTC-streamed remote desktops

**Status (2026-05-15):**
* Phase 1: AppleScript bridge — shipped at [tools/gfn_input_bridge.py](../tools/gfn_input_bridge.py).
  Now includes keepalive thread, `/run`, `/mouse_move`, `/click`, `/key`,
  smart-skip-when-not-frontmost.
* Phase 3 (Chrome extension MVP): shipped at
  [tools/phantominput-extension/](../tools/phantominput-extension/) and
  [tools/phantominput_host.py](../tools/phantominput_host.py). Uses
  `chrome.debugger` + CDP `Input.dispatchKeyEvent` — events flagged
  `isTrusted=true` at the Chromium level, bypassing GFN's filter.
* Unified Python wrapper: [tools/gfn_session.py](../tools/gfn_session.py)
  auto-detects which backend is reachable.

**Working name; rename before public launch.**

## The gap

Every WebRTC remote-desktop / cloud-streaming surface filters out
synthesized DOM events (`event.isTrusted == false`). This is a deliberate
defense against malicious JavaScript on the page, but it makes the
following classes of work effectively impossible today:

* QA / automation of cloud-gaming clients (GeForce NOW, NVIDIA App,
  Shadow.tech, AWS GameLift, Moonlight Web).
* QA / scripted testing of cloud-workstation surfaces (Citrix Receiver
  HTML5, VMware Horizon HTML5, Frame, Parsec Web, RunPod's web
  terminal).
* Browser-based remote desktop (Apache Guacamole, NoMachine Web,
  AnyDesk Web, Chrome Remote Desktop, NVIDIA's web client used by
  GFN-Creator and CloudXR).
* **AI agents that need to operate streamed VMs** — including the
  nvHive Wizard itself when it's used inside a cloud-GPU desktop.

Playwright, Puppeteer, Selenium, and every Chrome extension all live
above the `isTrusted` filter; none of them can drive the streamed
contents of a remote desktop. **There is no off-the-shelf solution
today.**

## The four layers of trustedness

| Layer | Mechanism | Trusted by streamer? | Install cost | Notes |
|---|---|---|---|---|
| 1. JS-synthesized DOM events | `element.dispatchEvent(...)` from a Chrome extension | ❌ no | zero | Dead end. What every browser-only tool tries first. |
| 2. OS-synthesized input events | `osascript` / `xdotool` / `SendInput` from a native helper | ✅ yes | accessibility permission | What this bridge does. Works today. |
| 3. Chrome DevTools Protocol (CDP) input | `chrome.debugger.attach` + `Input.dispatchKeyEvent` | ✅ yes | "Chrome is being debugged" banner | What Playwright uses. Cleanest extension path. |
| 4. Virtual HID device | `IOHIDUserDeviceCreate` / `uinput` / `ViGEm` | ✅ yes | kernel/system extension + notarization | Looks indistinguishable from a real USB keyboard / mouse. Ultimate compatibility. |

A real product would offer layers 2 + 3 in the OSS core and layer 4 as
a premium add-on for users who can't tolerate the focus-window
requirement of layer 2 or the debug banner of layer 3.

## Phased roadmap

### Phase 1 — macOS bridge (shipped)

* `tools/gfn_input_bridge.py` — HTTP server on `127.0.0.1:9876`, uses
  AppleScript via `osascript` to synthesize OS-level keystrokes.
* Safety: refuses to type unless the frontmost app is a known browser
  (`Google Chrome`, `Chromium`, `Safari`, `Arc`, `Brave Browser`).
* No external dependencies — uses only Python's stdlib + macOS's
  built-in `osascript`.
* Endpoints: `GET /health`, `POST /type`, `POST /key`.

This unblocks today's nvHive QA loop on macOS.

### Phase 2 — cross-platform native bridge

Goal: same HTTP surface, Linux + Windows + macOS support, single
distribution.

* **macOS**: keep AppleScript for the v0 path; migrate to Quartz
  (`CGEventPost`, `CGEventCreateKeyboardEvent`,
  `CGEventCreateMouseEvent`) so we can do mouse drag, scroll, and modifier
  combos cleanly. `pyobjc-framework-Quartz` is the only added dep.
* **Linux**: detect X11 vs Wayland. X11: `xdotool`. Wayland: `ydotool`
  or `python-uinput` directly (Wayland blocks most synthesizers, so
  uinput is the safer bet — and uinput keystrokes look exactly like a
  real USB keyboard to the kernel, which is also a half-step toward
  Phase 4).
* **Windows**: `SendInput` via `ctypes` (no dep) or `pyautogui`.
* Add **MCP server interface** so Claude Code, Cursor, Continue, and
  the nvHive Wizard itself can call it as a tool. Tool surface:
  - `type_text(text, allow_outside_browser=False)`
  - `press_key(key, modifiers=[])`
  - `mouse_move(x, y)`
  - `mouse_click(button, modifiers=[])`
  - `mouse_drag(x1, y1, x2, y2)`
  - `scroll(dx, dy)`
  - `screenshot()` — returns base64 PNG (so the agent can OCR / vision
    the streamed contents)
* Add **WebSocket interface** for low-latency streaming use (replay a
  recorded macro at 60 keystrokes/sec without the per-request HTTP
  overhead).
* Add **CLI**: `phantominput type "echo hi"`, `phantominput key Return`.
* Package as `pip install phantominput`. Ship a single PyPI wheel that
  works on the three platforms.

Effort: ~1-2 weeks of focused work for one engineer.

### Phase 3 — Chrome extension companion

The bridge alone solves the typing problem but requires the user to
keep the right Chrome window frontmost. A companion extension
automates that and adds streaming-aware niceties:

* Activates on known streaming domains
  (`play.geforcenow.com`, `*.parsec.app`, `*.frame.io`, `*.shadow.tech`,
  `app.runpod.io`, etc.).
* Brings the right tab forward before each bridge call.
* OCRs the streamed video element so the extension can say "click on
  the terminal" — because the streamed content is video pixels, not
  DOM, an LLM agent can't query elements there normally.
* "Record and replay" mode for QA scripts (recorder captures the user's
  real keystrokes/clicks; replay calls the bridge).
* "AI mode" exposes a CDP-like API to LLM agents — the same shape as
  Playwright's `page.type` / `page.click`, but routed through the
  trusted-input bridge so it works against streamed content.
* Future: dispatch input via CDP (Layer 3) when the user has accepted
  the debug banner — gives a no-bridge-needed path for users on
  Chromebooks or where the native bridge can't be installed.

Effort: ~3-4 weeks.

### Phase 4 — virtual HID device (optional, advanced)

For the highest tier of compatibility and stealth:

* macOS: `DriverKit` HID user-client (`IOHIDUserDeviceCreate` + the
  DriverKit replacement post-15.0). Requires Developer ID + notarization
  + the user clicking "Allow" in System Extensions.
* Linux: `python-uinput` or direct `/dev/uinput` ioctl — straightforward,
  no signing.
* Windows: ViGEmBus driver (existing OSS project) for game controllers;
  for keyboard, Interception or a custom HID-class driver.

A virtual HID device appears as a real USB keyboard / mouse to the OS.
Browsers (and the streaming WebRTC bridge) cannot distinguish it from
hardware. Eliminates:

* The accessibility-permission step on macOS.
* The "frontmost browser" constraint.
* Any future server-side detection of synthesized input (NVIDIA
  haven't shipped one yet, but they have the telemetry to).

Effort: ~2 months including signing pipelines. Reserve for v2.

## Distribution and naming

* Open core: layers 1-2 OSS under MIT (matches nvHive's license).
* Premium tier: AI-mode + record/replay + screenshot OCR.
* Distributed as a standalone product, **not** bundled into the nvHive
  CLI — different audience (QA engineers, AI tool builders) and
  different security story.
* Public name TBD. "PhantomInput" is the working name. Other candidates:
  *Conduit*, *Passthru*, *Untrusted* (deliberate), *Trace* (overloaded),
  *Liaison*, *Foothold*.

## Why this ships from nvHive's wedge

The strategic context (see auto-memory) anchors nvHive on
"rented-cloud-GPU-desktop renter." Those users are *exactly* the
audience that will need to automate streamed sessions:

* AI builders running agents on GFN-creator / RunPod / Vast / Lambda
  desktops want their LLM to be able to drive the remote terminal.
* QA teams testing cloud-workstation apps need scriptable input.
* AI-art / ComfyUI workflows often run on a streamed desktop and need
  batch operation.

Shipping PhantomInput from nvHive makes the wedge more credible
("we're the people who actually have to drive these things") and
generates inbound from a community nvHive hasn't yet reached
(automation engineers + browser-tooling devs).

## Concrete next steps

* [x] Phase 1 bridge — AppleScript HTTP bridge with `/type`, `/key`,
  `/run`, `/mouse_move`, `/click`, `/keepalive/{start,stop}` and the
  safety check on frontmost app.
* [x] Phase 1.5: keepalive thread with smart-skip — only jiggles the
  mouse when the target browser is already frontmost, so the user
  isn't yanked out of other apps every 20 seconds.
* [x] Phase 3 MVP: Chrome extension with `chrome.debugger` + CDP
  input, content script that auto-removes GFN's "rig ready" scrim,
  native messaging host that bridges localhost HTTP ↔ the extension.
* [x] Unified Python wrapper that auto-detects backend.
* [ ] Phase 2: cross-platform bridge for Linux (xdotool/uinput) and
  Windows (SendInput). Right now the extension covers cross-platform
  for Chrome-rendered streamers, but the native bridge is still
  macOS-only.
* [ ] Phase 3 polish: keepalive at the extension layer (currently
  unnecessary because CDP attachments register as user activity, but
  worth verifying empirically); OCR-based `wait_for_text(...)` so a
  script can wait for a prompt to appear before sending the next
  command.
* [ ] Phase 3 distribution: Chrome Web Store listing (this requires
  filling out the developer dashboard, screenshots, privacy policy,
  and review).
* [ ] Phase 4 evaluation: read up on DriverKit replacement story for
  macOS 26.x, scope the notarization pipeline. ETA 2 months including
  signing.

## Comparison: when to use which backend

| | AppleScript bridge | Chrome extension (PhantomInput) |
|---|---|---|
| One-time setup | `python3 tools/gfn_input_bridge.py` + grant macOS Accessibility | Load unpacked extension + `EXT_ID=… ./install-host.sh` |
| Per-session friction | Keep Chrome frontmost-ish | Click "Attach" once; debug bar appears |
| Cross-platform | macOS only | Chrome on macOS/Linux/Windows |
| Speed | ~50 ms / keystroke | <10 ms / keystroke |
| Background ops | Browser must be frontmost | Works while user is in another app |
| Detectability | Mouse cursor jiggles | Invisible (CDP events look real to the page) |

The `tools/gfn_session.py` wrapper picks the extension automatically
when both are reachable.

## Risks

* **WebRTC streamers can add input-shape detection later.** Mouse
  movement that's purely axis-aligned, or keystrokes with perfect
  uniform timing, are detectable as synthetic. Phase 2 should add
  realistic jitter to mouse paths and per-character typing variance.
* **macOS Accessibility prompt is jarring for new users.** Documenting
  it well in the install script will matter.
* **Streaming services may add ToS clauses against automation.** A
  legal review before public launch is non-optional. NVIDIA in
  particular has rights to gate GeForce NOW usage on their automation
  policy.
