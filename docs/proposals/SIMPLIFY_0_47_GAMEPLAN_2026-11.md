# 0.47 Simplify — gameplan (tickets for one session each)

Companion to [SIMPLIFY_0_47_DESIGN_2026-11.md](SIMPLIFY_0_47_DESIGN_2026-11.md).
The design says what and why; this page says exactly how, in tickets small
enough for one focused session with a smaller model. Tracking:
[issue 144](https://github.com/thatcooperguy/nvHive/issues/144). Written
2026-11-05 against `main` after S1 (`09c6c4e`) and the S2 working tree.

## How to work a ticket

1. Read the ticket, the design section it names, and every file it lists.
   Do not read the whole repo. Do not widen the ticket.
2. Rules that never bend: hide, fold or merge — never remove a capability or
   break a URL without a redirect (the Atohi decision); counts in words in
   README and docs (`tests/test_marketing_parity.py`); every `docs/<page>.md`
   stays named in README (`tests/test_docs_links.py`); none of the banned
   cloud-gaming product names anywhere (`tests/test_no_geforce_now_mentions.py`
   lists them — do not type them, not even in a comment);
   Python 3.11 syntax; keep each file's line endings (read bytes, detect
   `\r\n`, write back the same; never `sed -i`); new files use LF.
3. Off limits unless the ticket names them: `nvh/core/atohi.py`,
   `tests/test_atohi_*.py`, `nvh/config/settings.py`,
   `nvh/storage/repository.py`, `tests/conftest.py`, `.github/workflows/`,
   `web/package.json`, `web/package-lock.json`, `SECURITY.md` (its own
   commit, maintainers only), version strings.
4. Gate before you call a ticket done (from the repo root, Windows venv
   `.venv/Scripts/python`):
   ```bash
   python -m ruff check nvh/ tests/ --ignore E501,E402,N806,E702,F841
   python -m pytest tests/<each test file the ticket names>.py -q -p no:cacheprovider --timeout=300
   python -m pytest tests/test_docs_links.py tests/test_marketing_parity.py tests/test_no_geforce_now_mentions.py tests/test_web_surfaces.py -q -p no:cacheprovider
   cd web && npx tsc --noEmit -p tsconfig.json && npm test && npm run lint && npm run build && cd .. && git checkout -- web/next-env.d.ts
   ```
   Run pytest one file per process on Windows (the whole suite in one process
   stalls on loopback sockets). `npm run build` rewrites `web/next-env.d.ts`;
   restore it.
5. Land it: a CHANGELOG `[Unreleased]` bullet (Added / Changed / Fixed), a
   ROADMAP touch only if the ticket says so, one signed commit
   (`git commit -s`) ending with `Co-Authored-By: Claude Fable 5.1
   <noreply@anthropic.com>`, push to `main`, then confirm CI green
   (`gh run list --limit 3`). Report what you did on issue 144 in three
   sentences. Tag nothing.
6. If a step cannot be done as written, stop and say so in the issue comment
   with the file and line; do not improvise a different design.

## Wave S2 — one chat home (in flight; finish and verify)

**T2.1 Land S2.** Owner of record: the integrator. Files already edited:
`web/app/page.tsx` (Wizard default mode, `MODE_TABS` switch in the top bar,
`openWizardThread`, deep links), `web/app/wizard/page.tsx` (redirect stub),
`web/components/{ChatInput,LayoutShell,GlobalModals,Mascot,Sidebar}.tsx`,
`web/app/{agents,setup,schedules}/page.tsx`, `web/lib/types.ts`,
`web/next.config.js`, tests `test_web_surfaces.py`, `test_chat_history.py`,
`test_webui_e2e.py`, docs `WEBUI.md`, `MASCOT.md`, `ROADMAP.md`, CHANGELOG.
Done when: the gate above is green, and in a browser (`npm run dev -p 3100`
in `web/`) `/` opens in Wizard mode with the Wizard · Ask · Convene · Poll
switch, `/wizard?conversation=abc` lands on `/?c=abc`, `/wizard?starter=hi`
lands on `/?starter=hi`, the Agents page's "Use this agent" opens `/?profile=`,
and Enter sends in Ask mode while Shift+Enter adds a line.

**T2.2 Sidebar highlight follows a new wizard thread (S).** When the Wizard
creates its conversation on the first message it sets `?c=<id>` itself
(`web/components/chat/WizardThread.tsx`, the `url.searchParams.set(
conversationParam, conv.id)` site) but `web/app/page.tsx` keeps
`activeConvId` null, so the sidebar does not highlight the new thread until
it is clicked. Fix: WizardThread already dispatches
`CONVERSATIONS_CHANGED_EVENT`; in page.tsx's listener, when `mode ===
'wizard'`, read `new URLSearchParams(window.location.search).get('c')` and
`setActiveConvId` to it. Test: extend `tests/test_web_surfaces.py` with a pin
that page.tsx reads `params.get('c')` inside the conversations-changed
handler. Design §3.

**T2.3 Mode switch on narrow screens (S).** The switch in the top bar is
`hidden sm:flex`. Add the same four modes to the mobile hamburger drawer or a
compact select under `sm`. Files: `web/app/page.tsx` only. Test: a pin that
`MODE_TABS` is referenced twice (bar and narrow form).

**T2.4 Retire the `/query` and `/council` redirect note (docs, S).**
`docs/WEBUI.md` still says those pages "were removed in 0.42"; add that
`/wizard` joins them as a redirect and all three retire in 0.48 (O14).
Files: `docs/WEBUI.md`. Tests: `test_docs_links.py`, `test_web_surfaces.py`.

## Wave S3 — guided first run and plain-word paths

**T3.1 Pure first-run rules (S).** New `web/lib/firstRun.ts` (pure: no
imports from `@/lib/api`, no DOM) with `firstRunLines(status)` returning the
three lines of design §4 from `{ localModel: string | null, pulling: {model,
bytes} | null, gpu: boolean, keyCount: number, starters: string[] }`: line 1
the local model sentence (ready / pulling / no GPU), line 2 up to three
starters, line 3 "Keys, ports and packs live in Settings → Get started". New
`web/lib/firstRun.test.mjs` (Node `node --test`, like `web/lib/voice.test.mjs`)
covering the four states. Add `firstRun` to the Developing list in
`docs/WEBUI.md` and to the TESTING.md web row. Design §4.

**T3.2 Starters from the agent profiles (S).** `GET /v1/wizard/profiles`
returns `starter` per profile (typed in `web/lib/types.ts`,
`AgentProfileSchema.starter`). In `web/components/chat/EmptyState.tsx`
replace the hand-typed starter list with the first three non-empty `starter`
values of the loaded profiles (WizardThread already loads profiles once; pass
them down as a prop), falling back to the current list when none loaded.
Tests: `tests/test_web_surfaces.py` pin that EmptyState reads `starter`;
`node --test` for any pure helper. ROADMAP "Left from Wave C" item 19 is
closed by this (edit that bullet). Design §4.

**T3.3 The three lines in the empty state (M).** Wire T3.1 into
`web/components/chat/EmptyState.tsx`: the model line from `GET /v1/models`
(local rows) and the install job list (`getInstallJobs`), the GPU flag from
the platform block of `GET /v1/wizard/context`, the key count from
`GET /v1/health` or the providers roster — each call already exists in
`web/lib/api.ts`; add none. The third line links to `/setup`. Mascot: one tip
on the first run ("Ask me what this machine can do") through
`sayMascotTip` (`web/lib/mascot.ts`), once per session (sessionStorage key
`nvh.firstRun.tip`). Tests: `tests/test_web_surfaces.py` pins; `docs/
MASCOT.md` gains the first-run tip row. Design §4.

**T3.4 One next action on every banner (S).** `web/components/
ApiHealthBanner.tsx` ends with a button that runs the existing "Start the
API" bridge (`SystemConsole` exposes it: reuse, do not duplicate);
`web/components/WelcomeBackPanel.tsx` ends with one link ("Open the
console" or "Resume <thread>"). Tests: `tests/test_web_surfaces.py` pins the
two button labels. Design §4.

**T3.5 Plain-word refusal in the thread (S).** When a Wizard turn ends with
`fallback_reason: profile_local_only_provider_unavailable`
(`web/lib/api.ts` documents it), `web/components/chat/MessageBlock.tsx`
renders the sentence the CLI prints plus "Add a key in Settings →
Connections, or install a local model in Models" instead of the reason code.
Tests: `tests/test_web_wizard_refusal_status.py` (it already pins the refusal
rendering; extend). Design §4.

**T3.6 `x-api-key` as a second credential spelling (S, server).** In
`nvh/api/deps.py` `require_auth` accepts `x-api-key: <key>` exactly as
`Authorization: Bearer <key>` (same comparison, same 401 text). Tests:
`tests/test_api.py` TestDepsContract gains two cases (accepted, wrong key
401). Docs: `docs/INTEGRATIONS.md` Anthropic-SDK paragraph says
`ANTHROPIC_API_KEY=<key>` now works; `docs/CONFIGURATION.md` `HIVE_API_KEY`
row names both headers. SECURITY.md sentence is a maintainer commit — write
the sentence in the issue comment instead. Owner decision S-O7 default yes.
Design §4, ROADMAP "Left from Wave A".

**T3.7 Verify two launch targets (S, needs the maintainer's box).** Run `nvh
launch claude` and `nvh launch codex --print` for real; set `verified_on`
("2026-11-05") on those rows in `nvh/data/launch_targets.json`; the other
three stay null. Tests: `tests/test_launch.py` already branches on
`verified_on`; `docs/INTEGRATIONS.md` `### nvh launch` says which two are
verified. ROADMAP "Left from Wave D" item 23.

**T3.8 First-answer SMOKE assertion (M, server).** In the checks registry
(`nvh/integrations/diagnostics/checks.py`, SMOKE tier) add a check that posts
`POST /v1/wizard/chat` with a one-word prompt, no profile and no key, and
passes on a non-empty answer or on the plain-word refusal, failing only on a
5xx or an empty body; `skip` when nothing listens. Tests: `tests/
test_diagnostics_checks.py` with a fake client. Docs: `docs/TESTING.md`
SMOKE row. Design §4.

**T3.9 Follow-ups from 0.46 (one ticket each, all S).** (a) `Engine._build_
messages`: a transcript ending in an assistant or tool message is forwarded
as history (`if prompt or not history`) — ROADMAP "Left from Wave B" names the
hunk; tests in `tests/test_api_proxy.py`. (b) A file lock around
`Scheduler._save` (`nvh/core/scheduler.py`, reuse the existing lock helper in
the repo if one exists; `tests/test_scheduler.py`). (c) Session block pair
count: `GET /v1/budget/status?since=` returns `pairs` so an idle tab and a
failed-local-only tab differ; `web/lib/sessionCost.ts` reads it (`web/lib/
sessionCost.test.mjs`); ROADMAP "Left from Wave B/C". (d) Groq keyring
lookup gated by `NVH_USE_KEYRING` like the registry (`nvh/core/voice.py`
`_keyring_groq_key`; `tests/test_voice.py`). (e) ROADMAP "Carried from 0.45":
reword the POSIX kill bullet — the ubuntu run happened (CI run
41351349212). Each closes its ROADMAP bullet in the same commit.

## Wave S4 — consistency and screenshots

**T4.1 One header (M).** Every page under LayoutShell uses
`web/components/PageHeader.tsx` with the same eyebrow / title / subtitle
sizes; the chat home's top bar keeps its own height. Files: the eleven
`web/app/*/page.tsx` headers only (no behaviour). Tests: `tests/
test_web_surfaces.py` pin that each page imports `PageHeader`.

**T4.2 States (M).** Each page's empty, loading and error states end with one
next step in plain words (no reason codes, no "see logs"). Start from
`/vault`, `/schedules`, `/agents`, `/models`; list the strings you changed in
the commit message. Tests: pins for the four pages' empty-state sentences.

**T4.3 Screenshots (S, needs a running stack).** Take three 1440×900
captures per `docs/MAINTAINERS.md` (the chat home in Wizard mode with the
mascot and the cost pill; Models; a red approval card or the question card);
replace README's image and WEBUI.md's gallery; delete the seven stale files in
the same commit (`tests/test_docs_links.py` resolves every image). Design §5.

## Wave S5 — release 0.47

**T5.1 Release prep (integrator).** CHANGELOG `## [0.47.0] - <date>` with a
theme paragraph; ROADMAP 0.47 row "shipped", the 0.48 row (persistence
unification; the three redirects retire); version bump in `pyproject.toml`,
`nvh/__init__.py`, `web/lib/version.ts`; `docs/MAINTAINERS.md` "Before
tagging" 1–9 with evidence; SECURITY.md re-read in its own commit. The owner
tags.

## Order and parallelism

S2: T2.1 → T2.2, T2.3, T2.4 (any order). S3: T3.1 → T3.3; T3.2, T3.4, T3.5,
T3.6, T3.7, T3.8 and each T3.9 item are independent of one another but only
one session edits `web/` at a time (Dependabot #143 lands between sessions).
S4 after S3. S5 last. Owner items stay owner items: tags, O9's reason text,
Dependabot #140/#143, the mascot likeness.
