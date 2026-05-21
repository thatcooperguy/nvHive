/**
 * Content script for streaming-tab support.
 *
 * Runs on GeForce NOW pages. Responsibilities:
 *   1. Remove the "Your Gaming Rig is Ready!" scrim automatically so
 *      automated drivers don't have to babysit it. (We confirmed via
 *      DOM inspection that the streaming video is already live
 *      underneath the scrim — it's a pure visual block.)
 *   2. Auto-dismiss the "Are you still there?" idle warning by walking
 *      its CLOSE button via DOM, since that overlay also blocks input.
 *   3. Expose a tiny page-level helper at `window.__phantominput`
 *      so the background script can observe (read-only) what state the
 *      page is in.
 */

(function () {
  if (window.__phantominputContentInstalled) return;
  window.__phantominputContentInstalled = true;

  const log = (...args) => console.log('[PhantomInput]', ...args);

  function dismissScrim() {
    const scrim = document.querySelector('gfn-fullscreen-scrim-rig-ready-blocker');
    if (scrim) {
      scrim.remove();
      log('removed rig-ready scrim');
      return true;
    }
    return false;
  }

  function dismissIdlePrompt() {
    // "Are you still there?" countdown — clicking the page or pressing
    // any button dismisses it. Since we're now sending trusted input via
    // CDP, the next user-initiated click or key from the background
    // script will dismiss this. But if it appears while idle, we click
    // it via DOM as a fallback.
    const idle = document.querySelector('[class*="idle"], [class*="still-there"]');
    if (idle) {
      const btn = idle.querySelector('button');
      if (btn) {
        btn.click();
        log('clicked idle dismiss');
        return true;
      }
    }
    return false;
  }

  // Observe for the scrim appearing and auto-remove it.
  const observer = new MutationObserver(() => {
    dismissScrim();
    // Don't auto-dismiss idle prompts here; let the background
    // script's trusted input handle it. We only remove the scrim
    // because the scrim is a pure visual blocker with no functional
    // gate behind it (we verified).
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });

  // Also try once on load in case scrim was already present.
  dismissScrim();

  // Expose minimal page state for the background script to introspect.
  Object.defineProperty(window, '__phantominput', {
    value: Object.freeze({
      streamReady: () => !!document.getElementById('remote-video'),
      hasScrim: () => !!document.querySelector('gfn-fullscreen-scrim-rig-ready-blocker'),
      // Returns the video element's bounding rect so the agent can
      // translate "click at the bottom-left of the streamed Ubuntu
      // taskbar" into CDP page coordinates.
      videoRect: () => {
        const v = document.getElementById('remote-video');
        if (!v) return null;
        const r = v.getBoundingClientRect();
        return { x: r.x, y: r.y, w: r.width, h: r.height };
      },
    }),
    writable: false,
    configurable: false,
  });

  log('content script installed');
})();
