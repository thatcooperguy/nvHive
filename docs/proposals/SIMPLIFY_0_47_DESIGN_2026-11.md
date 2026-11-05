# 0.47 — Simplify: one home, a guided first run, a README that says what happens (design, 2026-11)

Status: proposal, written 2026-11-04 against `main` @ `415094c` (the 0.46.0
release commit; the tag is the owner's). It plans the next cycle in the shape
of [ADDS_0_46_DESIGN_2026-11.md](ADDS_0_46_DESIGN_2026-11.md): gate-able waves
with disjoint file ownership, owner decisions with the default that applies if
unanswered, and the follow-ups each wave leaves. Line anchors were read on the
day; the ones marked (unverified) come from the 0.46 design and were not
re-opened.

## 0. Why

The owner's words, 2026-11-04: "Plan it all out." "Also simplify the UI and
readme." "Make the readme awesome and the UI more apple style in simplicity for
user journey, it should just work."

"Apple-style simplicity" is read here as principles, not as borrowed visuals:

1. **One obvious place to start.** The browser opens on a chat that already
   works; everything else is one step away from it.
2. **Progressive disclosure.** Operator detail (providers, ports, usage, the
   system console) is a click deeper, never gone. The command palette and the
   URL keep every page reachable.
3. **Defaults that work.** A first answer needs no key, no model choice and no
   setting; the local model picked for the GPU or the free tier answers.
4. **Plain words.** Labels say what a person would say ("Memory", "Models",
   "Settings"), errors say the one next step, and the README says what will
   happen before it says what nvHive is made of.
5. **Quiet system state.** Health and cost are visible in one line, not a
   dashboard; nothing blinks unless it needs the person.
6. **Nothing is lost.** Every route, feature and capability stays. The Atohi
   decision (2026-09-16) binds this cycle: nvHive hides, folds and merges
   surfaces; it never retires or renames a capability, and no URL stops
   working without a redirect.

## 1. Goals and non-goals

Goals for 0.47:

- The WebUI has one chat home at `/` (the shell merge held out of 0.46 as
  C1b), five top-level destinations and a Settings group, and a first run that
  reaches a first answer with one click.
- Every empty state and every failure a first-time user can hit names the one
  next step, in plain words, in the UI and in the CLI.
- The README reads in ten seconds: what it is, the one command, what happens
  next, what you get, where the docs are.
- The 0.46 follow-ups that cause a dead end or a silent loss ship; the rest are
  dispositioned (deferred with a release, or handed to the owner).

Non-goals (declined, not deferred): a visual restyle (new colours, fonts or
icon set), a native app, a second theme system, removing the council and
compare modes, removing `/setup`, `/providers`, `/integrations`, `/system`,
`/analytics` or `/settings` as pages, and anything in the ROADMAP's Non-goals.
Persistence unification (two transports, two persistence paths behind the
chat; the 0.46 design's "honest scope") is 0.48, not 0.47: promising both the
merge and the unification repeats the 0.44 over-scope.

## 2. Target information architecture

Today (`web/components/Sidebar.tsx:270-282`, eleven entries in one list): AI
Wizard, Agents, Models, Setup, My Computer, Memory Vault, AI Connections,
Usage, Preferences, Developer Tools, Schedules. Two of them (`/` and
`/wizard`) are chat surfaces; the sidebar lists one of them.

After (five destinations and a Settings group; every route unchanged):

| Sidebar | Route | Today's label | Note |
|---|---|---|---|
| Chat | `/` | AI Wizard (`/wizard`) and the chat page (`/`) | one home; Wizard is the default mode, Council and Compare are modes in the composer (§3) |
| Models | `/models` | Models | unchanged |
| Agents | `/agents` | Agents | unchanged; draws `starter` / `accent` (follow-up 19) |
| Memory | `/vault` | Memory Vault | label only |
| Schedules | `/schedules` | Schedules | unchanged |
| Settings ▸ | — | — | a disclosure group at the bottom; expanded when any member is active |
| ▸ Get started | `/setup` | Setup | the full onboarding page stays; the first run (§4) links here for the long form |
| ▸ Connections | `/providers` | AI Connections | provider keys and health |
| ▸ Developer tools | `/integrations` | Developer Tools | SDK, proxies, MCP, `nvh launch` |
| ▸ This computer | `/system` | My Computer | hardware, services, console |
| ▸ Usage | `/analytics` | Usage | spend and history |
| ▸ Preferences | `/settings` | Preferences | theme, cache, browser data, Costs |

Rules: the hrefs stay in `Sidebar.tsx` (tests/test_webui_e2e.py:163-170
expects nav links to every page; tests/test_web_surfaces.py:229 pins
`href: '/schedules'`; tests/test_model_manager_ui.py:69 reads the file), so the
group is a collapsed section of the same list, not a second component. The
command palette (`web/components/CommandPalette.tsx`) lists all twelve pages by
both names for one release. Label changes are presentation; the ROADMAP feature
rows keep their capability names and gain "present-but-hidden" only where a
page leaves the first screen (none does — the group is one click).

Mobile: the same list; the group starts collapsed.

## 3. The one chat home (C1b)

Mechanics as the 0.46 design wrote them (ADDS :170-199, :482), with the
cheaper variant chosen where it is equivalent:

- `web/app/page.tsx` (1,246 lines today: modes `single` / `council` /
  `compare`, its own Sidebar mount and h-11 bar, Share, `HardwareWidgetCompact`)
  becomes `<Suspense><ChatSurface/></Suspense>`. `web/components/chat/
  ChatSurface.tsx` owns `mode`, URL sync (`?mode`, `?c`, `?prompt`) and
  renders `<WizardThread>` (default) or `<CouncilThread>` (the council and
  compare engine moved verbatim from page.tsx:664-990 (unverified)).
- `/` joins LayoutShell branch B (`web/components/LayoutShell.tsx:119-144`
  is the branch that lets `/` and `/setup` manage their own chrome): the
  page's own Sidebar mount and bar go; `SessionCostPill`, the GPU pill and
  Share move into LayoutShell's bar behind `isChatPage`.
- `web/app/wizard/page.tsx` is deleted and `web/next.config.js` gains
  `/wizard → /` (one release) beside the `/query` and `/council` redirects,
  whose comment ("Remove in 0.43") is rewritten to the O14 date. Every inbound
  `/wizard?conversation=` link moves to `/?c=`: `LayoutShell.tsx:93-95`,
  `web/app/setup/page.tsx` (two sites), `web/app/agents/page.tsx`,
  `web/components/Mascot.tsx`, the `/schedules` Open thread link (anchors per
  ADDS :174-175, unverified today).
- Enter sends in every mode (O9's last clause); Esc stops a stream; the
  composer's mode menu replaces the three mode shortcuts' visible buttons but
  keeps the shortcuts (`mode-ask`, `mode-convene`, `mode-poll`,
  page.tsx:1016-1018).
- Server: `ConversationCreateRequest.mode` pattern gains `wizard`
  (ADDS :166, unverified); `_persist_wizard_turn` fills the token and cost
  columns (ADDS :189, unverified) so the sidebar's cost column reads the same
  for wizard rows.

Pins that move (Appendix A lists them all): tests/test_chat_history.py:250-275
literals in page.tsx and LayoutShell.tsx; tests/test_release_hardening.py:796
and :902 (LayoutShell still mounts SystemConsole and DebugReportButton — kept);
tests/test_web_surfaces.py (`/wizard` mounts WizardThread → `/` mounts
ChatSurface which mounts WizardThread); tests/test_single_provider_table.py's
glob (new files are scanned automatically); tests/test_webui_e2e.py nav list.
New `tests/test_web_single_surface.py`: next.config.js carries the three
redirects with the O14 date, no `href="/wizard` survives in `web/`, `/`
renders `ChatSurface`, the composer's Enter rule.

## 4. The guided first run and "first answer in under a minute"

Where the person lands after `install.sh` (`nvh services start --open`,
install.sh:331-359): the chat home. Today the thread's empty state is a
hand-typed starter list (ROADMAP "Left from Wave C"); `/setup` is a 6,294-line
page with eight sections (Persistent Storage, GPU Detection, Model Picker,
Local AI Setup, AI Studio Packs, ComfyUI, Cloud Providers, Quick Test, then
"SETUP COMPLETE").

The first run becomes three lines inside the chat's empty state, each with one
action, derived from the machine's state (not typed):

1. **Your local model** — "`<model>` is ready on your GPU" (from `GET
   /v1/models` and the tier), or "Pulling `<model>` (`<n>` GB) — you can ask
   the free cloud tier meanwhile" with a progress line (the install job), or
   on a no-GPU box "No GPU here: the free cloud tier answers; add a key in
   Settings → Connections for more".
2. **Ask anything** — three starter chips drawn from the agent profiles'
   `starter` field (the field the API already returns; follow-up 19), e.g.
   "What can this machine run?", "Check my services", "Summarise a file".
3. **Later** — "Keys, ports and packs live in Settings → Get started" (the
   link to `/setup`).

The mascot says one tip on the first run ("Ask me what this machine can do")
and nothing more until the person types; the tip rules are in docs/MASCOT.md.

Server-side, the path to a first answer gets plain-word refusals where a
first-time user can stall:

- No local model and no key: the Wizard's `fallback_reason` already names
  `profile_local_only_provider_unavailable`; the thread renders it as one
  sentence with the next step, not the reason code.
- Anthropic-SDK clients: `require_auth` reads `x-api-key` as a second
  spelling of the same credential (owner item; default yes, §7), so the most
  common SDK configuration stops being a 401.
- `nvh launch`: `claude` and `codex` are run once on the maintainer's box and
  get a `verified_on` date; the other three keep "unverified" and the
  `/integrations` snippet says what that means in one line.
- The `ApiHealthBanner` and `WelcomeBackPanel` each end with one action
  ("Start the API", "Open the console").

What keeps it working: `nvh test`'s SMOKE tier already posts a real
`/v1/query` when something listens; it gains a "first answer" assertion
through the Wizard path (`POST /v1/wizard/chat` with no profile and no key
set, expecting an answer or the plain-word refusal, never a 500), and the web
e2e test opens `/` and asserts the three first-run lines render against a
mocked health response.

## 5. The README

Today: 112 lines, a 918-character AI Wizard paragraph that is 0.46 release
notes (README.md:34), a hand-typed count at :36 that
tests/test_marketing_parity.py tolerates only while it equals the catalog
(its docstring says state no count), two tables that are a third of the page,
and screenshots from April–August 2026 that show a sidebar and a theme that no
longer exist (none shows the mascot or the chat the installer opens on).

The new README (the draft is in this cycle's scratch, landed by Wave S1):

1. **Hero** — name, one sentence ("nvHive turns an NVIDIA machine you rent or
   own into a working AI lab with one command. No root, no Docker, nothing
   lost on reconnect."), the three badges.
2. **The one command**, then one line of alternatives as links (Linux desktop
   session, Windows, macOS, `pip`).
3. **What happens next** — four numbered lines a reader can verify: a home
   that survives resets and the ports it will use; the GPU read and a model
   that fits pulled (skippable); Ollama → API → WebUI each checked healthy
   before the browser opens; the first question in the chat.
4. **One screenshot** — the chat home with the mascot and the cost pill, taken
   after Wave S2 (until then the existing Model Manager image stays, because
   tests/test_docs_links.py resolves every image and WEBUI.md embeds the same
   files).
5. **What you get** — five short blurbs in the user's words: a chat that knows
   the machine (tools, questions, memory, images, voice); models that fit the
   GPU before download; memory that survives (vault, RAG); automations
   (schedules, agents, council); builders' doors (OpenAI and Anthropic paths,
   MCP, `nvh launch`). No counts beside the guarded nouns; no release notes.
6. **Who it is for** — rented GPU desktops (RunPod, Lambda, Vast), DGX Spark;
   Windows on Arm best-effort. The Spark playbooks keep their "not yet
   exercised on hardware" sentence.
7. **Requirements** — four bullets.
8. **If something breaks** — the Debug Report button first, then the two
   commands.
9. **Docs** — one grouped line of links naming all twelve `docs/<page>`
   literals (tests/test_docs_links.py:82-85 is a substring test; a table is
   not required), plus CONTRIBUTING.
10. **Trust** — license, trademarks, security, the two notes.

Voice rules: concrete verbs, no superlatives, no digit before `providers`,
`models`, `free`, `cabinets`, `tools`, `personas`, `agent(s)`, no banned
product names (tests/test_no_geforce_now_mentions.py), relative links only to
files that exist, install URLs unchanged. The README is also the PyPI long
description (pyproject.toml:9): keep the HTML to the centred hero.

Honesty constraint: remote tags and PyPI stop at v0.42.0 while `main` is
0.46.0. The README describes this repository; the `pip install nvhive` line
says "the published release follows the tags" until the owner tags 0.46.0.

## 6. Waves

Each wave is one PR-sized landing on `main`, gated before and after its fix
pass, with the integrator folding CHANGELOG `[Unreleased]` and ROADMAP.

**Wave S1 — README, sidebar group, redirect comment (docs and web chrome; can land first).**
Files: README.md; docs/GETTING_STARTED.md (the alternatives line and the
first-five-minutes list agree with README); docs/WEBUI.md (the sidebar row and
the page table's first column use the new labels; the `/wizard` row stays until
S2); web/components/Sidebar.tsx (five entries + the Settings disclosure group;
hrefs unchanged); web/components/CommandPalette.tsx (both names for one
release); web/next.config.js (comment only: the O14 date); tests:
tests/test_webui_e2e.py nav assertion if it enumerates labels,
tests/test_web_surfaces.py (a pin that the group holds the six hrefs),
tests/test_docs_links.py and tests/test_marketing_parity.py unchanged and green.
Gate: the standard list (Appendix B). Left: screenshots (S4).

**Wave S2 — the one chat home (C1b).** Files per §3: web/app/page.tsx,
web/components/chat/ChatSurface.tsx (new), chat/CouncilThread.tsx (new),
web/app/wizard/page.tsx (deleted), LayoutShell.tsx, Sidebar.tsx (the Chat
entry's href), next.config.js (the `/wizard` redirect), the five inbound-link
files, nvh/api/server.py (`mode` pattern), nvh/integrations/wizard/chat.py
(`_persist_wizard_turn` columns); tests: test_chat_history.py pins,
test_web_surfaces.py, new test_web_single_surface.py, test_webui_e2e.py; docs:
WEBUI.md (one chat row; "all threads on `/?c=<id>`"), MASCOT.md (one composer),
ARCHITECTURE.md:149 (`/council` reference), CONFIGURATION.md `wizard-meta`
block if the tail changes. Precondition: O9 confirmed (§7) and Dependabot #143
merged or deferred (one writer in `web/` at a time). Gate: standard plus
`npm run build` route table shows `/` and no `/wizard`.

**Wave S3 — the guided first run and plain-word paths.** Files:
web/components/chat/EmptyState.tsx (the three derived lines), web/lib/firstRun.ts
(pure; Node test), web/app/agents/page.tsx (`starter` / `accent` drawn),
web/components/ApiHealthBanner.tsx and WelcomeBackPanel.tsx (one action each),
web/components/Mascot.tsx (first-run tip), nvh/api/deps.py (`x-api-key`,
behind the owner decision), nvh/integrations/wizard/chat.py (refusal sentence),
nvh/data/launch_targets.json (`verified_on` for `claude` and `codex` after a
real run), nvh/cli/checks (the first-answer SMOKE assertion), plus the 0.46
follow-ups taken in §8 (12, 13, 14, 22 where S2 did not cover it, 29); tests
beside each; docs: WEBUI.md, CONFIGURATION.md (the `x-api-key` sentence),
SECURITY.md (own commit), INTEGRATIONS.md (`verified_on`). Gate: standard plus
the new SMOKE assertion offline (skip) and against a live API on the
maintainer's box.

**Wave S4 — polish and screenshots.** One header, one spacing scale, one set of
empty / loading / error states across the twelve pages (a pass, not a
restyle); the three screenshots retaken at 1440×900 per docs/MAINTAINERS.md
(the chat home, Models, a red approval card or question card) and the stale
seven removed from README/WEBUI.md in the same commit (tests/test_docs_links.py
resolves images); WEBUI.md gallery. Files: web/components/PageHeader.tsx and
the page files' headers, docs/screenshots/*, README.md (image), docs/WEBUI.md.
Gate: standard; the image set rule in MAINTAINERS.md:347-349.

**Wave S5 — release 0.47.** CHANGELOG `## [0.47.0]`, ROADMAP rows (0.47 shipped
text and the 0.48 row: persistence unification, redirect retirement), version
bump, MAINTAINERS "Before tagging" 1–9, SECURITY.md re-read (own commit). The
owner tags.

Order: S1 → (S2 ∥ nothing else in `web/`) → S3 → S4 → S5. S1 and S3's server
hunks (`x-api-key`, the refusal sentence, the SMOKE assertion) touch no `web/`
file and may run beside S2.

## 7. Owner decisions (defaults apply if unanswered)

- **S-O1 — O9, the shell merge.** Default: **yes** — the 2026-11-04 ask ("one
  simple user journey") is read as the confirmation 0.46 lacked; S2 starts
  after S1 lands. If declined, S2 is replaced by "Chat → `/wizard`" in the
  sidebar and the `/` page keeps its modes; both ROADMAP texts are prepared.
- **S-O2 — O14 date.** Default: `/query`, `/council` and `/wizard` redirects
  retire together in 0.48 (one release after the merge lands).
- **S-O3 — Sidebar names.** Default: Chat, Models, Agents, Memory, Schedules,
  Settings ▸ Get started / Connections / Developer tools / This computer /
  Usage / Preferences.
- **S-O4 — `/setup` stays a page** under Settings → Get started; the chat's
  first run is the short form. Default: yes.
- **S-O5 — Screenshots** retaken after S2 with the placeholder sprite in them
  (the likeness sign-off is separate). Default: yes.
- **S-O6 — Dependabot #143** (next 16.4.3, react 19.3.1) merged before S2 or
  after S4, never during; #140 closed (superseded by `mcp<3`). Default: the
  owner merges #143 before S2; the integrator does not merge PRs.
- **S-O7 — `x-api-key`** as a second credential spelling on `require_auth`.
  Default: yes, in S3, with a SECURITY.md sentence and one test.
- **S-O8 — Tags.** v0.46.0 at `415094c` (and the untagged v0.44.0, v0.44.1,
  v0.45.0); the README's `pip install` line stays honest until then. Owner
  only; ask before any PyPI-producing tag.
- **S-O9 — No restyle.** The theme tokens, fonts and icons stay; S4 is
  consistency, not a new look. Default: yes.
- **S-O10 — First-run starters** come from the agent profiles' `starter`
  field (hidden specialists keep deciding who answers). Default: yes.

## 8. Follow-ups carried from 0.46

Numbering follows the backlog inventory of this cycle (ROADMAP "Known
follow-ups" and the 0.46 design's open items). **Do in 0.47:** 12 (a transcript
ending in an assistant or tool message is forwarded as history), 13 (a file
lock around `Scheduler._save`), 14 (the session block's pair count; `convene`
`cost_known`), 19 (`starter` / `accent` drawn; the empty state derived), 22
(thread links to `/?c=`), 23 (`claude` and `codex` verified on the maintainer's
box), 26 (one real `voice` install asserting `voice_status` after the
download), 29 (the Groq keyring gate), 33 (reword the POSIX kill bullet: the
ubuntu run happened), O1's research (which launch targets validate through
`GET /v1/models`). **Defer to 0.48:** 15, 16, 17 (cards rehydrated — with
persistence unification), 18, 20, 21, 24, 25, 28, 30, 32, 34, 36 (persistence
unification). **Owner:** 1–11 as listed in §7 and the ROADMAP (mascot
likeness, qwen3.6 tier table, `TOOL_CALL:` in fences, LLM7 tier, the NemoClaw
blueprint consumer). **Watch:** 31 (the `filelock` floor when litellm lifts
its pin).

## 9. Rejected

- A new "Home" dashboard page before the chat: a second thing to understand
  before the first answer. The chat's empty state is the home.
- Merging `/providers`, `/integrations`, `/system`, `/analytics` and
  `/settings` into one tabbed page in 0.47: 2,744 lines of page code moved for
  a grouping the sidebar can express; a tab strip component shared by the six
  pages is S4's option if the group proves insufficient.
- Removing council and compare modes: capabilities, not surfaces (Atohi rule).
- A README with a feature matrix or counts: the guards and the ten-second test
  both argue against it.
- Taking screenshots before S2: they would show the surface S2 removes.

## 10. Risks

- O9 unanswered again → S2 waits; S1, S3's server hunks and S4's state pass
  still ship, and the README's journey does not name the landing route.
- The merge collides with pinned literals (Appendix A); the C1b fixer moves
  them in the same change or the gate is red.
- `web/` has one writer at a time: S2 and Dependabot #143 cannot overlap.
- The README promises `pip install nvhive` features that PyPI (0.42.0) lacks
  until the owner tags; the line says so until then.
- Screenshots need a running stack on the merged shell; they are last.
- "First answer in under a minute" is a design goal measured by the SMOKE
  assertion, not a README claim; the README says what the installer checks.
- Hardware-dependent items (Spark playbooks, Phase 4, RTX Spark) stay
  unverified; the README keeps the sentence.

## Appendix A — pins the waves move

- tests/test_chat_history.py:250-275 — `void renameConversation(`,
  `void deleteConversation(`, `void pinConversation(`, `createConversation(`,
  `appendConversationMessage(` in page.tsx → chat/ChatSurface.tsx or
  chat/CouncilThread.tsx; `/wizard?conversation=` in LayoutShell.tsx → `/?c=`.
- tests/test_release_hardening.py:796, :902 — LayoutShell mounts SystemConsole
  and DebugReportButton (kept as is).
- tests/test_web_surfaces.py — `/wizard` mounts WizardThread (→ `/` renders
  ChatSurface → WizardThread); `href: '/schedules'` in Sidebar NAV (kept);
  `nvh.voice.readAloud` in BROWSER_KEYS (kept).
- tests/test_webui_e2e.py:163-170 — nav links to all pages (the group keeps
  every href).
- tests/test_model_manager_ui.py:69 — reads Sidebar.tsx (check the literal).
- tests/test_single_provider_table.py — globs web/app/**/*.tsx and web/lib/*.ts
  (new files are scanned; no hand-typed provider table).
- tests/test_docs_links.py:45-56, :82-85 — every image and link resolves;
  README names every `docs/<page>.md`.
- tests/test_marketing_parity.py:29-31 — no digit before the guarded nouns.

## Appendix B — gate (mirrors `.github/workflows/ci.yml`)

```bash
python -m ruff check nvh/ tests/ --ignore E501,E402,N806,E702,F841
python -m pytest tests/<each touched file>.py -q          # one process per file on Windows
python -m mypy nvh/sandbox nvh/catalog --strict --follow-imports=silent --ignore-missing-imports
python scripts/gen_commands_doc.py --check && python scripts/gen_config_doc.py --check
cd web && npx tsc --noEmit -p tsconfig.json && npm test && npm run lint && npm run build
python -m pytest tests/test_no_geforce_now_mentions.py tests/test_docs_links.py tests/test_marketing_parity.py -q
```

Plus, for S2 and S4, the full suite in lanes and `git checkout -- web/next-env.d.ts`
after the build; for S5, docs/MAINTAINERS.md "Before tagging" 1–9.
