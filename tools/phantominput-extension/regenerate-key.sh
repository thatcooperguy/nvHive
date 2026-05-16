#!/bin/bash
# Regenerate the RSA keypair used to derive the extension's deterministic ID.
#
# Only re-run this if:
#   - You're cutting a public release and want a clean key under the
#     production org's control.
#   - The current private key was compromised.
#
# Everyday devs should NOT re-run this. The committed key + ID let any
# clone of the repo produce the same extension ID, which is what makes
# the native messaging host manifest portable.

set -euo pipefail
cd "$(dirname "$0")"

mkdir -p .keys
openssl genrsa -out .keys/extension_key.pem 2048 2>/dev/null
openssl rsa -in .keys/extension_key.pem -pubout -outform DER \
    -out .keys/extension_pub.der 2>/dev/null

PUB_B64=$(base64 -i .keys/extension_pub.der | tr -d '\n')
EXT_ID=$(openssl dgst -sha256 -binary .keys/extension_pub.der \
            | xxd -p | tr -d '\n' | head -c 32 \
            | tr '0-9a-f' 'a-p')

echo "$PUB_B64" > .keys/extension_pub.b64
echo "$EXT_ID" > .keys/extension_id.txt

# Update manifest.json's "key" field in place.
python3 - <<PY
import json, pathlib
p = pathlib.Path("manifest.json")
data = json.loads(p.read_text())
data["key"] = "$PUB_B64"
p.write_text(json.dumps(data, indent=2) + "\n")
print(f"manifest.json key field updated")
PY

echo
echo "New extension ID: $EXT_ID"
echo
echo "Commit the changes:"
echo "  git add manifest.json .keys/extension_pub.b64 .keys/extension_id.txt"
echo "  # do NOT commit .keys/extension_key.pem — that's the private key."
echo "  git diff --cached"
