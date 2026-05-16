# PhantomInput Chrome extension (dev preview)

Inject **trusted** keyboard and mouse events into WebRTC-streamed remote
desktops (GeForce NOW, Parsec, Frame, etc.) for QA, automation, and
AI-agent control.

Why this exists: streaming clients filter out JS-synthesized DOM events
(`event.isTrusted == false`) so a normal Chrome extension cannot type
into the streamed desktop. This extension uses `chrome.debugger` +
Chrome DevTools Protocol `Input.dispatchKeyEvent` — those events are
flagged trusted at the Chromium level, so they pass through the
streamer's filter.

## Install (dev mode)

```bash
# 1. Open chrome://extensions and enable "Developer mode" (top right).
# 2. Click "Load unpacked" and pick this directory:
#    /Users/ccooper/nvh/tools/phantominput-extension
# 3. Note the extension ID Chrome assigns (looks like 32 random letters).
# 4. Register the native messaging host:
EXT_ID=<that-id> ./install-host.sh
```

That's it. The extension auto-activates on `play.geforcenow.com`.

## Usage

### From the popup
Click the PhantomInput toolbar icon → **Attach**.
Chrome will show a "PhantomInput started debugging this browser" bar at
the top of the GFN tab. That's normal — it's required for the trusted
input path. Click **Detach** to remove it.

### From a local script
After installing the host manifest, hit the HTTP server:

```bash
curl -s http://127.0.0.1:9877/health
# {"ok": true, "host_alive": true}

# Type a command in the GFN tab (auto-finds the GFN tab):
curl -s -X POST http://127.0.0.1:9877/run \
  -H 'content-type: application/json' \
  -d '{"command": "echo hello from CDP"}'
```

### Endpoints (HTTP host on `127.0.0.1:9877`)

| Method | Path | Body | Effect |
|---|---|---|---|
| GET  | `/health` | — | host alive check |
| GET  | `/status` | — | list tabs + attached state |
| POST | `/attach` | `{"urlContains":"..."}` | attach `chrome.debugger` to the matching tab |
| POST | `/detach` | `{"urlContains":"..."}` | detach |
| POST | `/type`   | `{"text":"..."}` | type literal text |
| POST | `/run`    | `{"command":"..."}` | type + Enter |
| POST | `/key`    | `{"key":"Enter","modifiers":0}` | press a named key |
| POST | `/click`  | `{"x":100,"y":200,"button":"left"}` | click at page coords |
| POST | `/move`   | `{"x":100,"y":200}` | move cursor |
| POST | `/screenshot` | — | base64 PNG of the tab viewport |

## Architecture

```
external caller (curl / Claude / CI)
        │
        ▼
HTTP 127.0.0.1:9877  ←─── tools/phantominput_host.py (native msg host)
        │  stdio (Native Messaging)
        ▼
Chrome extension service worker  ─── chrome.debugger + CDP
        │  Input.dispatchKeyEvent / dispatchMouseEvent
        ▼
target tab (GFN page)
        │  events have isTrusted=true
        ▼
GFN's WebRTC bridge → cloud Ubuntu desktop ✅
```

## Why this beats the AppleScript bridge

| | AppleScript bridge | PhantomInput extension |
|---|---|---|
| Cross-platform | macOS only | Wherever Chrome runs |
| Permission gate | macOS Accessibility (System Settings) | One-time debugger banner per session |
| Speed | ~50 ms/keystroke (osascript overhead) | <10 ms/keystroke |
| Background ops | Requires browser frontmost | Works while user is in another tab/app |
| Detectability | Visible mouse cursor jiggle | None (events are real to GFN) |

## Roadmap

See [../../docs/phantominput-roadmap.md](../../docs/phantominput-roadmap.md)
for the full picture — Phase 3 (this), Phase 4 (virtual HID device for
zero-banner full-stealth mode), and the productization arc.

## Known gaps in this MVP

- Single host per Chrome profile (manifest is per-extension-id).
- Screenshot is base64 over JSON; not great for >5 MB pages but fine
  for the GFN page itself.
- Click coordinates are page-relative, not screen-relative. The
  content script's `window.__phantominput.videoRect()` gives you the
  page-relative bounding box of the streamed video so callers can
  translate "click bottom-left of the Ubuntu taskbar" → CDP coords.
- No keepalive yet at the extension layer (it's not needed — CDP
  input doesn't have the same idle problem the AppleScript bridge
  has, because we're attached as a debugger). If needed later, easy
  to add a service worker alarm.
