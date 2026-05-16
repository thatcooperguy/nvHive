/**
 * PhantomInput service worker.
 *
 * Architecture:
 *   - User clicks the extension action OR a managed page calls the
 *     extension via chrome.runtime.sendMessage.
 *   - We use chrome.debugger.attach() on the target tab and call
 *     CDP `Input.dispatchKeyEvent` / `Input.dispatchMouseEvent`.
 *   - CDP-dispatched input events are marked `isTrusted=true` at the
 *     Chromium level, so they pass through GFN's WebRTC input filter
 *     (which rejects normal JS-synthesized events).
 *
 * Local-control bridge:
 *   The service worker also accepts WebSocket connections from
 *   ws://127.0.0.1:9877 — this lets a local process (Claude Code, a
 *   QA runner, a CI script) send commands to the extension without
 *   going through chrome.runtime messaging.
 *
 * MV3 service workers cannot host servers directly, so we use a
 * companion native messaging host (`tools/phantominput_host.py`)
 * that listens on the local port and forwards messages to/from the
 * extension via stdio.
 */

const DEBUGGER_VERSION = '1.3';

// Track which tabs we've attached to so we can clean up.
const attachedTabs = new Set();

async function ensureAttached(tabId) {
  if (attachedTabs.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, DEBUGGER_VERSION);
  attachedTabs.add(tabId);
}

async function detach(tabId) {
  if (!attachedTabs.has(tabId)) return;
  try {
    await chrome.debugger.detach({ tabId });
  } catch (e) {
    // ignore: tab may already be gone
  }
  attachedTabs.delete(tabId);
}

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId !== undefined) {
    attachedTabs.delete(source.tabId);
  }
});

/**
 * Type a string of text into the target tab as a sequence of trusted
 * keyDown/char/keyUp events. Each char is sent as a separate event
 * pair; this matches how a real user typing is observed by the page.
 */
async function typeText(tabId, text) {
  await ensureAttached(tabId);
  for (const ch of text) {
    if (ch === '\n') {
      await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
        type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13,
      });
      await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
        type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13,
      });
      continue;
    }
    // For printable characters, use the `char` event type which CDP
    // documents as the proper way to insert literal text. This avoids
    // having to map every Unicode char to a (key, code) pair.
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
      type: 'char', text: ch,
    });
  }
}

async function pressKey(tabId, key, modifiers = 0) {
  await ensureAttached(tabId);
  const opts = { type: 'keyDown', key, code: key, modifiers };
  // Common keys need a virtual key code so the page's keydown handler
  // sees event.keyCode correctly.
  const VK_MAP = {
    Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46,
    ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39,
    F1: 112, F2: 113, F3: 114, F4: 115, F5: 116, F6: 117,
    F7: 118, F8: 119, F9: 120, F10: 121, F11: 122, F12: 123,
  };
  if (VK_MAP[key] !== undefined) opts.windowsVirtualKeyCode = VK_MAP[key];
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', opts);
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
    ...opts, type: 'keyUp',
  });
}

async function moveMouse(tabId, x, y) {
  await ensureAttached(tabId);
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
    type: 'mouseMoved', x, y, button: 'none',
  });
}

async function clickAt(tabId, x, y, button = 'left') {
  await ensureAttached(tabId);
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
    type: 'mousePressed', x, y, button, clickCount: 1,
  });
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
    type: 'mouseReleased', x, y, button, clickCount: 1,
  });
}

async function screenshotTab(tabId) {
  await ensureAttached(tabId);
  const r = await chrome.debugger.sendCommand(
    { tabId }, 'Page.captureScreenshot', { format: 'png', captureBeyondViewport: false },
  );
  return r.data; // base64 PNG
}

/**
 * Find the first tab matching a URL pattern (or a single open tab on a
 * known streaming domain). Used by the local bridge so callers don't
 * need to know tab ids.
 */
async function findTargetTab(urlContains) {
  const tabs = await chrome.tabs.query({});
  if (urlContains) {
    return tabs.find(t => (t.url || '').includes(urlContains));
  }
  // Default: any GFN tab.
  return tabs.find(t => /play\.geforcenow\.com/.test(t.url || ''));
}

// chrome.runtime messaging — used by both the popup and the native
// messaging host. Each message is { op, tabId?, urlContains?, ...args }.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      let targetId = msg.tabId;
      if (!targetId) {
        const t = await findTargetTab(msg.urlContains);
        if (!t) {
          sendResponse({ ok: false, error: 'no matching tab' });
          return;
        }
        targetId = t.id;
      }

      switch (msg.op) {
        case 'attach':
          await ensureAttached(targetId);
          sendResponse({ ok: true, tabId: targetId });
          return;
        case 'detach':
          await detach(targetId);
          sendResponse({ ok: true, tabId: targetId });
          return;
        case 'type':
          await typeText(targetId, msg.text || '');
          sendResponse({ ok: true, tabId: targetId, chars: (msg.text || '').length });
          return;
        case 'run': {
          const cmd = (msg.command || '').replace(/[\r\n]+$/, '');
          await typeText(targetId, cmd);
          await pressKey(targetId, 'Enter');
          sendResponse({ ok: true, tabId: targetId });
          return;
        }
        case 'key':
          await pressKey(targetId, msg.key, msg.modifiers || 0);
          sendResponse({ ok: true, tabId: targetId });
          return;
        case 'move':
          await moveMouse(targetId, msg.x, msg.y);
          sendResponse({ ok: true, tabId: targetId });
          return;
        case 'click':
          await clickAt(targetId, msg.x, msg.y, msg.button || 'left');
          sendResponse({ ok: true, tabId: targetId });
          return;
        case 'screenshot': {
          const data = await screenshotTab(targetId);
          sendResponse({ ok: true, tabId: targetId, data });
          return;
        }
        case 'status':
          sendResponse({
            ok: true,
            attached: Array.from(attachedTabs),
            tabs: (await chrome.tabs.query({})).map(t => ({ id: t.id, url: t.url })),
          });
          return;
        default:
          sendResponse({ ok: false, error: `unknown op: ${msg.op}` });
      }
    } catch (err) {
      sendResponse({ ok: false, error: String(err && err.message || err) });
    }
  })();
  return true; // keep the channel open for async sendResponse
});

// Native messaging host — pumps messages between the local bridge
// process and this service worker. The host is started by the user once
// (or auto-spawned on first message).
let nativePort = null;
function connectNativeHost() {
  if (nativePort) return nativePort;
  try {
    nativePort = chrome.runtime.connectNative('com.nvhive.phantominput');
    nativePort.onMessage.addListener(async (msg) => {
      // Forward to runtime message handler and echo result back.
      chrome.runtime.sendMessage(msg, (resp) => {
        nativePort.postMessage({ requestId: msg.requestId, response: resp });
      });
    });
    nativePort.onDisconnect.addListener(() => { nativePort = null; });
  } catch (e) {
    nativePort = null;
  }
  return nativePort;
}

// Lazy: only connect when something asks us to.
chrome.runtime.onStartup.addListener(() => { /* don't auto-connect */ });
