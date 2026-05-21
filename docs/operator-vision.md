# nvHive Operator — agent control plane for streamed remote desktops

**Status:** vision doc. Pieces of this are shipping under the working
name "PhantomInput" today; this doc captures the product framing once
those pieces consolidate.

## The thesis

Every cloud-GPU workflow today eventually lands a user on a streamed
remote desktop — a Linux VM rendered into a `<video>` element over
WebRTC. GeForce NOW Creator, NVIDIA Omniverse Cloud, RunPod's web
shell, Vast.ai consoles, Parsec, Frame, Citrix Web, VMware Horizon
HTML5, Apache Guacamole. **Every one of these surfaces is unautomatable
today** because the WebRTC clients filter out synthesized DOM input
(`event.isTrusted == false`) as a security defense.

That means:

* **AI agents can read the screen but can't operate it.** A vision LLM
  can OCR the streamed desktop, but `page.type()` from Playwright does
  nothing.
* **QA has no story.** You cannot script a smoke test of a cloud
  workstation app the way you'd script Selenium against a website.
* **CI integration is impossible.** A nightly job that boots a fresh
  cloud GPU, installs your software, runs your test suite, and reports
  back — that pipeline cannot exist on most of these platforms.

The market is small today because nobody can automate it. Build the
input layer and the market expands behind it: AI agents that operate
cloud desktops, automated QA for streamed apps, CI/CD against cloud
VMs that don't expose SSH.

## What "Operator" is

Three layers, each independently useful:

### 1. Input layer (this exists now, in two backends)

OS-level or CDP-level input injection that bypasses the streamer's
`isTrusted` filter. Same API across:

* **Chrome extension** (interactive workstations) — `chrome.debugger`
  + CDP. The most ergonomic for a human-in-the-loop dev/QA workflow.
* **CDP-over-debug-port** (headless CI) — `chrome --remote-debugging-port`,
  external Python/Node client connects via WebSocket, no extension
  install required. **Critical for headless build/test agents.**
* **macOS AppleScript bridge** (fallback) — works without Chrome
  debugging permissions when interactive macOS Accessibility is the
  only available trust path.

A single `Session` Python class auto-detects which is reachable and
exposes one API: `session.run("nvh --version")`, `session.click(x,y)`,
`session.screenshot()`. Caller code is identical across backends.

### 2. Agent layer

The Operator is exposed as an **MCP server**, so any agent (Claude
Code, Cursor, Continue, future Wizard, custom LangGraph) gets these
tools without writing transport code:

```
operator.attach(streaming_url)        # find or open the streaming tab
operator.run(command, wait_until=...) # type + Enter, wait for prompt
operator.wait_for_text("$", timeout)  # OCR-poll until terminal prompt
operator.screenshot()                  # base64 PNG of the streamed desktop
operator.click_text("Firefox")         # OCR the screen, click the label
operator.region_text(rect)             # OCR a specific rect
operator.session_health()              # is the stream alive? authenticated?
operator.record_macro(name)            # capture a sequence for replay
operator.replay_macro(name, vars)      # parameterized replay
```

The agent doesn't know about CDP or extensions. It says "run this on
the cloud VM" and the Operator routes through whatever backend is up.

### 3. Distribution layer

**Three packagings**, same core:

* **`nvhive operator`** — built into the nvHive CLI on day 1.
  Discoverable for existing users. `nvh operator attach`,
  `nvh operator run`, `nvh operator gui` (opens a control panel).
  Bundles the Chrome extension as an unpacked load + auto-installer.
* **`pip install operator`** (or whatever the public name is) —
  standalone Python package for QA engineers, CI workflows, and
  agent toolchain authors who don't care about nvHive specifically.
  Same API, same MCP server, no nvHive dependency.
* **Chrome Web Store listing** — the extension as a public install
  for non-developer users. Click-to-add. UI exposes the same control
  surface as the Python CLI.

## The headless CI story (the killer use case)

Today, if you want to test your cloud-workstation app on a real
streamed session as part of CI, you can't. Tomorrow:

```yaml
# .github/workflows/streamed-app-test.yml
- name: Boot fresh GFN session
  run: |
    pip install nvhive-operator
    operator boot --provider gfn --image envoy-test --wait-ready

- name: Run install + smoke tests
  run: |
    operator run "curl -sSL .../install.sh | bash"
    operator wait_for_text "installation complete" --timeout 600
    operator run "nvh selfcheck --output /tmp/bundle.json"
    operator copy_from /tmp/bundle.json ./artifacts/

- name: Verify WebUI loads
  run: |
    operator run "nvh webui --port 3000 &"
    operator wait_for_text "API server ready" --timeout 60
    operator screenshot --output ./artifacts/webui-loaded.png

- uses: actions/upload-artifact@v4
  with:
    path: ./artifacts/
```

**This pipeline cannot be built with any existing tool.** Selenium
can't drive WebRTC streams. Playwright can drive the WebRTC client's
UI shell but not the streamed desktop's content. Vision LLMs can see
but not act.

This is what `nvhive operator` enables.

## Auth + identity for headless

A real CI use case needs Operator to log in to GFN / RunPod / Vast.ai
on its own. Three paths:

* **Token-based** (where the streamer supports it) — RunPod has an
  API key; we use it to provision the session via REST, then drive
  via CDP.
* **Cookie / refresh-token replay** (where they only allow web auth)
  — capture the user's cookies from an interactive session once,
  store encrypted, replay in CI sessions until they expire.
* **SSO automation** — for enterprise customers, integrate with their
  IdP via SAML/OIDC programmatically.

Document this in a separate "auth.md" once we pick a launch streamer.
For v1 GFN-Creator (the cohort the founder is targeting anyway) and
RunPod cover the immediate need.

## Pricing thoughts

Three pieces:

* **OSS core** — input layer, Python wrapper, MCP server. MIT.
* **Operator Cloud** — hosted version that handles the headless Chrome
  pool, browser-fingerprint management, auth tokens, screenshot
  storage. SaaS, billed per session-minute. This is where the money is.
* **Enterprise tier** — audit logging, RBAC, SOC2, on-prem deployment,
  the virtual-HID-device option for sites that prohibit the debug
  banner. Annual contract.

## Why this is nvHive's to ship

The founder's wedge ("rented cloud GPU desktop renter") is *exactly*
the audience that hits this problem the moment they try to scale.
nvHive shipping the Operator does three things:

1. **Validates the wedge** — "we ship the tool we ourselves need to
   automate the workflow we sell into." Founders + customers see the
   same surface.
2. **Generates leads from a new community** — AI tool builders,
   QA engineers, CI/CD pros find Operator first, learn about nvHive
   second. Different audience than nvHive's direct users today; the
   funnel widens.
3. **Differentiates from generic AI assistants** — Claude Code,
   Cursor, etc. can suggest commands. Only nvHive (with Operator)
   can actually run them on the cloud GPU desktop.

## What's not in this doc

* Implementation details for each backend — see
  `docs/phantominput-roadmap.md` for the technical roadmap, and the
  source under `tools/` for what exists today.
* Specific UI for the Chrome extension popup or nvHive WebUI
  integration — those are design exercises after we've validated
  the headless CI story with a real customer.
* Pricing details — needs talking to actual prospects first.

## Concrete near-term steps

1. **Ship CDP-over-debug-port backend** (this PR's add-on) so headless
   Chrome usage works today.
2. **Ship MCP server** so Claude / Cursor / Wizard can drive it via
   tool calls (this PR's add-on).
3. **Write a single headless-CI demo** — GitHub Actions workflow that
   boots GFN-Creator (or RunPod), runs `nvh selfcheck`, posts the
   bundle as an artifact. Filmed video + blog post = launch pad.
4. **Rename the working title** before any public release. "PhantomInput"
   is fine for dev; the public name should align with the nvHive
   brand (candidates: "Operator", "Conduit", "Liaison", "Footing",
   "Pulse"). Pick one before the demo lands.
