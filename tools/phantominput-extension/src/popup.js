/**
 * Popup UI — minimal dev-mode controls for PhantomInput.
 *
 * Most callers will drive the extension via the native messaging host
 * (so they don't have to click anything). This popup is for verifying
 * the extension is alive and for one-shot tests.
 */

const $ = (id) => document.getElementById(id);

async function refresh() {
  const resp = await chrome.runtime.sendMessage({ op: 'status' });
  if (!resp.ok) {
    $('status').textContent = 'no response';
    return;
  }
  const gfn = resp.tabs.find(t => /geforcenow\.com/.test(t.url || ''));
  const attached = resp.attached.length > 0;
  $('status').innerHTML = gfn
    ? `<b>GFN tab</b>: ${gfn.id}<br>${attached ? '<span style="color:#76B900">●</span> attached' : '○ detached'}`
    : 'no GFN tab open';
}

async function call(op, args = {}) {
  const resp = await chrome.runtime.sendMessage({ op, urlContains: 'geforcenow.com', ...args });
  if (!resp.ok) {
    $('status').textContent = 'error: ' + (resp.error || 'unknown');
  } else {
    await refresh();
  }
}

$('attach').addEventListener('click', () => call('attach'));
$('detach').addEventListener('click', () => call('detach'));
$('status-refresh').addEventListener('click', refresh);
$('test-echo').addEventListener('click', () => call('run', { command: 'echo "hi from phantominput"' }));

refresh();
