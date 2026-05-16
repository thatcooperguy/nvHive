#!/bin/bash
# Install the PhantomInput native messaging host manifest into Chrome's
# native messaging directory, then print the extension ID we need to
# bake into it.
#
# Run AFTER loading the unpacked extension in chrome://extensions so we
# can pull the assigned extension ID. If you haven't loaded the extension
# yet, run this with EXT_ID="..." to set it explicitly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOST_SCRIPT="${REPO_ROOT}/tools/phantominput_host.py"

if [ ! -f "$HOST_SCRIPT" ]; then
  echo "ERROR: cannot find $HOST_SCRIPT" >&2
  exit 1
fi
chmod +x "$HOST_SCRIPT"

case "$(uname)" in
  Darwin)
    NM_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    ;;
  Linux)
    NM_DIR="$HOME/.config/google-chrome/NativeMessagingHosts"
    ;;
  *)
    echo "ERROR: unsupported platform $(uname)" >&2
    exit 1
    ;;
esac
mkdir -p "$NM_DIR"

EXT_ID="${EXT_ID:-}"
if [ -z "$EXT_ID" ]; then
  cat <<EOF >&2
EXT_ID is not set. Steps:
  1. Open chrome://extensions, enable Developer mode.
  2. Click "Load unpacked" and select:
     ${REPO_ROOT}/tools/phantominput-extension
  3. Copy the extension ID Chrome shows on the card.
  4. Re-run:  EXT_ID=<that-id> $0
EOF
  exit 2
fi

MANIFEST="${NM_DIR}/com.nvhive.phantominput.json"
cat > "$MANIFEST" <<JSON
{
  "name": "com.nvhive.phantominput",
  "description": "PhantomInput native bridge — exposes localhost HTTP that drives the Chrome extension via Native Messaging.",
  "path": "${HOST_SCRIPT}",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://${EXT_ID}/"
  ]
}
JSON

echo "Wrote $MANIFEST"
echo
echo "Next: in the extension popup or via fetch, request a native"
echo "messaging connection. The host will start serving on:"
echo "  http://127.0.0.1:9877/"
echo
echo "To test:"
echo "  curl http://127.0.0.1:9877/health"
echo "  curl -X POST http://127.0.0.1:9877/status"
