#!/bin/bash
# Operator one-shot installer.
#
# What it does:
#   1. Reads the deterministic extension ID computed once at extension
#      build time (committed to tools/phantominput-extension/.keys/).
#   2. Registers the native messaging host manifest in Chrome's
#      NativeMessagingHosts dir for the user's profile.
#   3. Starts the native messaging host (it'll be re-spawned by Chrome
#      each time the extension connects; this is just a sanity-check
#      that python3 + the host script work).
#   4. Opens chrome://extensions for the user.
#   5. Prints clear instructions (one-time: enable developer mode +
#      load unpacked + pick the extension directory).
#   6. Polls the host's /installed endpoint until the extension's
#      service worker has connected.
#   7. Confirms end-to-end and exits.
#
# Usage:
#   ./tools/operator_install.sh
#
# Idempotent — safe to re-run. The manifest gets re-written each time
# (so it always points at the current absolute path of the host script).

set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

EXT_DIR="$PWD/tools/phantominput-extension"
HOST_SCRIPT="$PWD/tools/phantominput_host.py"
EXT_ID_FILE="$EXT_DIR/.keys/extension_id.txt"

if [ ! -f "$EXT_ID_FILE" ]; then
  echo "ERROR: $EXT_ID_FILE not found — run keypair generation first." >&2
  echo "  cd tools/phantominput-extension && ./regenerate-key.sh" >&2
  exit 1
fi
EXT_ID="$(cat "$EXT_ID_FILE")"

if [ ! -x "$HOST_SCRIPT" ]; then
  chmod +x "$HOST_SCRIPT"
fi

case "$(uname)" in
  Darwin)
    NM_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    OPEN_CMD="open"
    ;;
  Linux)
    NM_DIR="$HOME/.config/google-chrome/NativeMessagingHosts"
    OPEN_CMD="xdg-open"
    ;;
  *)
    echo "ERROR: unsupported platform $(uname)" >&2
    exit 1
    ;;
esac
mkdir -p "$NM_DIR"

MANIFEST="$NM_DIR/com.nvhive.phantominput.json"
cat > "$MANIFEST" <<JSON
{
  "name": "com.nvhive.phantominput",
  "description": "PhantomInput native bridge — exposes localhost HTTP that drives the Chrome extension via Native Messaging.",
  "path": "$HOST_SCRIPT",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$EXT_ID/"
  ]
}
JSON

echo "Wrote native host manifest: $MANIFEST"
echo "Extension ID:               $EXT_ID"
echo

# Sanity: can we even spawn the host?
if ! /usr/bin/python3 -c "import json, http.server" 2>/dev/null; then
  echo "ERROR: /usr/bin/python3 is missing stdlib modules. Is Python 3 fully installed?" >&2
  exit 1
fi

echo "Opening chrome://extensions for you …"
"$OPEN_CMD" "chrome://extensions" >/dev/null 2>&1 || true
echo

cat <<INSTRUCTIONS

   ─────────────────────────────────────────────────────────────────────
                       ONE-TIME LOAD (≈ 20 seconds)
   ─────────────────────────────────────────────────────────────────────

   In the chrome://extensions tab that just opened:

   1.  Toggle "Developer mode" ON          (top-right corner)
   2.  Click   "Load unpacked"             (top-left)
   3.  Pick this directory:

        $EXT_DIR

   That's it. The deterministic extension ID is baked into the manifest
   so you don't have to copy anything. The native messaging host is
   already registered, and the extension will auto-connect on load.

   Waiting for the extension's service worker to connect …

INSTRUCTIONS

# Poll /installed until 200 or timeout
DEADLINE=$(($(date +%s) + 300))   # 5 min
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  STATUS="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9877/installed 2>/dev/null || echo 000)"
  if [ "$STATUS" = "200" ]; then
    echo
    echo "✅ Extension is loaded and connected."
    curl -s http://127.0.0.1:9877/installed | /usr/bin/python3 -m json.tool 2>/dev/null || true
    echo
    echo "You're done. From here, any tool can drive the streaming session:"
    echo "  curl -s -X POST http://127.0.0.1:9877/status"
    echo "  python3 tools/gfn_session.py run 'echo hello'"
    exit 0
  fi
  if [ "$STATUS" = "000" ]; then
    # Host isn't running yet — extension hasn't connected at all.
    printf "."
  else
    printf "·"
  fi
  sleep 2
done

echo
echo "❌ Timeout waiting for extension. Things to check:"
echo "  - chrome://extensions shows 'PhantomInput (dev)' with ID $EXT_ID"
echo "  - The extension's 'Inspect views: service worker' link opens without error"
echo "  - System logs:  ls -lat \"$HOME/Library/Logs/Google/Chrome/\""
exit 1
