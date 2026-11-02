'use client';

import Link from 'next/link';
import { createContext, useContext, useEffect, useState } from 'react';
import PlanDetails from '@/components/PlanDetails';
import PlaybookRunLog from '@/components/PlaybookRunLog';
import {
  fetchApiBlob,
  type WizardChatToolCall,
  type WizardToolExecuteResult,
  type WizardToolSchema,
} from '@/lib/api';
import { costPill, formatCardCost } from '@/lib/cost';
import {
  NEEDS_TERMINAL_HINT,
  PRIVILEGED_DISABLED_NOTE,
  PRIVILEGED_SUDO_NOTE,
  WIZARD_TOOL_OUTCOME_CHARS,
  cardChrome,
  imageResult,
  isPendingStatus,
  isPrivilegedCall,
  isPrivilegedDisabled,
  isQuestionCall,
  needsTerminal,
  planLines,
  planSummary,
  questionAllowsFreeText,
  questionChoices,
  questionText,
  rememberResult,
  terminalCommand,
  unwrapToolResult,
  vaultNoteHref,
  type CardChrome,
  type PlaybookRunState,
  type ToolCardStatus,
} from '@/lib/privileged';
import type { WizardImageResult } from '@/lib/types';

/**
 * How a question card hands its answer back to the thread. The thread
 * (chat/WizardThread.tsx) provides one per message — the message id is bound
 * there, so the card needs nothing but the call and the text — and its
 * `answerQuestion` turns the text into the next user message plus the
 * `tool_results` entry that closes the card in history. Without a provider
 * (a card rendered outside the thread) the Answer button is not offered;
 * Skip still is. `busy` is true while a turn is streaming: the Answer button
 * is disabled then, as the composer's Send is, instead of swallowing the click.
 */
export type QuestionAnswerHandler = (call: WizardChatToolCall, answer: string) => void;
export interface QuestionAnswerContextValue {
  answer: QuestionAnswerHandler;
  busy: boolean;
}
export const QuestionAnswerContext = createContext<QuestionAnswerContextValue | null>(null);

/**
 * One tool the Wizard proposed, as a card the user must act on.
 *
 * Three chromes, all of which still need a click (or are already settled):
 *  - ordinary confirm — themed border, "Run" / "Skip". Every safety class
 *    that is not exactly `privileged` lands here, including one this build
 *    has never heard of. Since 0.46 a confirm tool with a planner
 *    (`generate_image`) also carries `plan`: its one-line `summary` (the
 *    backend, the cost verdict) renders as the plan line above the buttons
 *    without changing the chrome — a plan does not make a card privileged;
 *  - privileged (sudo) — red border, a PRIVILEGED pill, the one-line note
 *    that it runs with sudo on this machine, the server's exact commands in a
 *    <pre> (with what changes, the undo and any warning) above the buttons,
 *    and a destructive "Approve and run". The plan and the approval token
 *    arrive on the surfaced call; nothing is read from the model's arguments;
 *  - disabled — `NVH_ALLOW_PRIVILEGED=0`: a muted line naming the variable
 *    and NO run button at all (Skip stays while the card is still pending).
 *
 * A fourth card is not a tool run at all: the question card (`ask_user`,
 * 0.46) — the question, up to six choice chips, a free-text box when the
 * server allows one, "Answer" / "Skip". Answer sends the text as the next
 * user message (never to /v1/wizard/tools/execute); Skip records "not
 * answered". `QuestionCard` below.
 *
 * Results are shape-aware where the generic summary line is not enough: a
 * `generate_image` success renders the image inline (fetched with the API
 * headers — the outputs route is auth-gated — and shown from an object URL
 * that is revoked on unmount), with the path, the backend and model, and
 * the three-state cost pill; a `remember` success links "Open in vault".
 *
 * The `needs-terminal` outcome is a hand-off, not a failure: sudo wanted a
 * password, so nvHive did not run the command and shows it copyable instead.
 * nvHive never prompts for, sees or stores a password.
 */
export default function ToolCard({
  call,
  schema,
  status,
  result,
  detail,
  run,
  privilegedEnabled,
  onConfirm,
  onDismiss,
}: {
  call: WizardChatToolCall;
  schema: WizardToolSchema | undefined;
  status: ToolCardStatus;
  result?: string;
  /** Raw execute response: plan / command / hint / disabled live here. */
  detail?: WizardToolExecuteResult;
  /** The folded `playbook-run` job, once a confirmed `playbook_install` started one. */
  run?: PlaybookRunState;
  privilegedEnabled?: boolean;
  onConfirm: () => void;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const questionAnswer = useContext(QuestionAnswerContext);
  // Every call that reaches a card needs a click, whatever the catalog says
  // its safety class is — the server already ran what it was willing to.
  const description = schema?.description ?? call.name;
  const question = isQuestionCall(call);

  // The server marks a surfaced privileged call (`call.privileged`) as well as
  // its execute answers, so the red chrome never depends on the local catalog.
  const privileged = isPrivilegedCall(schema, detail) || isPrivilegedCall(undefined, call);
  const disabled = call.disabled === true || isPrivilegedDisabled(schema, detail, privilegedEnabled);
  // A card the kill switch has turned off reads as disabled before it is ever
  // clicked, even though nothing has run. It keeps its Skip button while the
  // underlying status is still pending, so the user can settle it and the
  // mascot stops asking.
  const effectiveStatus: ToolCardStatus = disabled && isPendingStatus(status) ? 'disabled' : status;
  const chrome = cardChrome(privileged ? 'privileged' : schema?.safety_class, effectiveStatus, { question });
  const skippable = chrome.clickable || isPendingStatus(status);
  // The exact commands: the server's dry run, delivered on the surfaced call
  // (or on an unconfirmed execute answer). Displayed for approval only —
  // never re-derived here, never fetched, and never the model's `arguments`.
  const planSource = planLines(call).length > 0 ? call : detail;
  const handoff = needsTerminal(detail);
  const command = terminalCommand(detail);
  // 0.46: a confirm-class planner's verdict rides on the surfaced call. The
  // line is the server's `summary` verbatim (backend, cost verdict, GPU
  // note); a plan without one shows backend · model · the cost pill text.
  // Informational: the chrome above stays the confirm chrome.
  const plan = call.plan && typeof call.plan === 'object' ? call.plan : null;
  const planCost = plan ? costPill(plan.cost_usd, plan.cost_known, formatCardCost) : null;
  const planLine = planSummary(plan, planCost?.text);
  // The handler's answer once the card settled Done, for the shape-aware
  // result blocks (the inline image, the vault link).
  const outcome = status === 'ok' ? unwrapToolResult(detail) : null;
  const image = outcome ? imageResult(outcome) : null;
  const saved = outcome ? rememberResult(outcome) : null;

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard blocked (insecure origin / denied permission) — the command
      // is on screen in a selectable block, so there is nothing to recover.
    }
  };

  if (question) {
    return (
      <QuestionCard
        call={call}
        status={status}
        chrome={chrome}
        description={description}
        result={result}
        onAnswer={questionAnswer ? answer => questionAnswer.answer(call, answer) : null}
        busy={questionAnswer?.busy ?? false}
        onDismiss={onDismiss}
      />
    );
  }

  return (
    <div
      className="mt-2 rounded-md border p-2 text-xs"
      style={{
        background: chrome.background ?? 'var(--bg-subtle)',
        borderColor: chrome.borderColor ?? 'var(--border)',
      }}
      aria-label={privileged ? `${call.name} — ${PRIVILEGED_SUDO_NOTE}` : undefined}
    >
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>
          {call.name}
        </span>
        {chrome.classBadge && (
          <span
            className="rounded-sm border px-1 py-px text-[9px] font-mono font-semibold uppercase tracking-[0.14em]"
            style={{ borderColor: chrome.borderColor ?? undefined, color: chrome.borderColor ?? undefined }}
          >
            {chrome.classBadge}
          </span>
        )}
        <span
          className="text-[9px] font-mono uppercase tracking-[0.14em]"
          style={{ color: chrome.badgeColor }}
        >
          {chrome.badge}
        </span>
      </div>
      <div className="mt-1" style={{ color: 'var(--text-secondary)' }}>
        {description}
      </div>
      {chrome.privileged && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: chrome.borderColor ?? undefined }}>
          {PRIVILEGED_SUDO_NOTE}
        </div>
      )}
      {/* The plan and its notes come BEFORE the buttons in DOM order, so a
          screen reader (and the tab order) reaches what is being approved
          before the control that approves it. */}
      {planLine && (
        <div
          className="mt-1 font-mono text-[10px]"
          style={{ color: 'var(--text-secondary)' }}
          title={plan?.reason || undefined}
          data-cost-state={planCost?.state}
          role="note"
        >
          plan: {planLine}
        </div>
      )}
      <PlanDetails source={planSource} borderColor={chrome.borderColor} />
      {run && <PlaybookRunLog run={run} borderColor={chrome.borderColor} />}
      {Object.keys(call.arguments).length > 0 && (
        // Shown as received. A planner card's arguments include the
        // server-pinned `profile` / `profile_home`; Run sends this exact
        // object back (WizardThread `runTool`), never a trimmed copy.
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
          args: {JSON.stringify(call.arguments)}
        </div>
      )}
      {skippable && (
        <div className="mt-2 flex items-center justify-end gap-1">
          <button
            type="button"
            onClick={onDismiss}
            className="rounded-sm border px-2 py-1 text-[10px] font-mono transition-colors hover:bg-[var(--bg-hover)]"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
            title="Leave this tool un-run"
          >
            Skip
          </button>
          {chrome.clickable && (
            <button
              type="button"
              onClick={onConfirm}
              className={
                chrome.privileged
                  ? 'rounded-md px-2 py-1 text-[10px] font-mono font-semibold text-white transition-opacity hover:opacity-90'
                  : 'btn-primary px-2 py-1 text-[10px] font-mono'
              }
              style={chrome.privileged ? { background: chrome.borderColor ?? undefined } : undefined}
              title={chrome.privileged ? PRIVILEGED_SUDO_NOTE : undefined}
            >
              {chrome.runLabel}
            </button>
          )}
        </div>
      )}
      {disabled && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
          {PRIVILEGED_DISABLED_NOTE}
        </div>
      )}
      {handoff && (
        <div className="mt-1">
          <div className="flex items-center justify-between gap-2">
            <code
              className="min-w-0 flex-1 overflow-x-auto rounded-sm border px-1.5 py-1 font-mono text-[10px] whitespace-pre"
              style={{
                background: 'var(--bg-card)',
                borderColor: 'var(--border)',
                color: 'var(--text-primary)',
              }}
            >
              {command || '(no command was returned)'}
            </code>
            {command && (
              <button
                type="button"
                onClick={() => void copyCommand()}
                className="shrink-0 rounded-sm border px-2 py-1 text-[10px] font-mono transition-colors hover:bg-[var(--bg-hover)]"
                style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
              >
                {copied ? 'Copied' : 'Copy command'}
              </button>
            )}
          </div>
          <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
            {detail?.hint ?? NEEDS_TERMINAL_HINT}
          </div>
        </div>
      )}
      {image && (
        <GeneratedImage
          image={image}
          alt={typeof call.arguments.prompt === 'string' && call.arguments.prompt.trim()
            ? call.arguments.prompt.trim()
            : 'the generated image'}
        />
      )}
      {result && !disabled && !image && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-secondary)' }}>
          {result}
        </div>
      )}
      {saved && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
          <Link
            href={vaultNoteHref(saved.id)}
            className="underline underline-offset-2 hover:opacity-80"
            style={{ color: '#76B900' }}
            title={saved.path}
          >
            Open in vault
          </Link>
          {typeof saved.redacted === 'number' && saved.redacted > 0 && (
            <span> · {saved.redacted} secret-looking span{saved.redacted === 1 ? '' : 's'} redacted before the write</span>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The question card (`ask_user`). Nothing runs: the question text, the
 * choice chips (at most six — the server's cap), a free-text box unless the
 * server said `allow_free_text: false` (shown anyway when there are no
 * chips, or the card could not be answered), and Answer / Skip. A chip
 * fills the answer; typing clears the chip. Answer hands the text to the
 * thread, which sends it as the next user message and closes the card in
 * history; Skip is the ordinary dismiss, recorded as "not answered". A
 * counter appears when the answer is longer than the history summary keeps
 * (`WIZARD_TOOL_OUTCOME_CHARS`): the message itself goes through whole.
 */
function QuestionCard({
  call,
  status,
  chrome,
  description,
  result,
  onAnswer,
  busy,
  onDismiss,
}: {
  call: WizardChatToolCall;
  status: ToolCardStatus;
  chrome: CardChrome;
  description: string;
  result?: string;
  /** `null` outside a thread: the Answer button is not offered. */
  onAnswer: ((answer: string) => void) | null;
  /** A turn is streaming: Answer stays visible but disabled until it ends. */
  busy: boolean;
  onDismiss: () => void;
}) {
  const text = questionText(call) || description;
  const choices = questionChoices(call);
  const freeText = questionAllowsFreeText(call) || choices.length === 0;
  const [picked, setPicked] = useState<string | null>(null);
  const [typed, setTyped] = useState('');
  const answer = typed.trim() || picked || '';
  const pending = isPendingStatus(status);
  // Offered: the button is drawn. Answerable: a click (or Enter) goes
  // through — not while the thread is streaming another turn.
  const offered = pending && onAnswer !== null;
  const canAnswer = offered && !busy;
  const overCap = answer.length > WIZARD_TOOL_OUTCOME_CHARS;

  const submit = () => {
    if (canAnswer && answer && onAnswer) onAnswer(answer);
  };

  return (
    <div
      className="mt-2 rounded-md border p-2 text-xs"
      style={{ background: 'var(--bg-subtle)', borderColor: 'var(--border)' }}
      role="group"
      aria-label={`${call.name} — the Wizard has a question for you`}
    >
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>
          {call.name}
        </span>
        <span
          className="text-[9px] font-mono uppercase tracking-[0.14em]"
          style={{ color: chrome.badgeColor }}
        >
          {chrome.badge}
        </span>
      </div>
      <div className="mt-1 text-sm" style={{ color: 'var(--text-primary)' }}>
        {text}
      </div>
      {pending && choices.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1" role="group" aria-label="Choices">
          {choices.map(choice => {
            const active = picked === choice && !typed.trim();
            return (
              <button
                key={choice}
                type="button"
                aria-pressed={active}
                onClick={() => {
                  setPicked(choice);
                  setTyped('');
                }}
                className="rounded-sm border px-2 py-1 text-[10px] font-mono transition-colors hover:bg-[var(--bg-hover)]"
                style={{
                  borderColor: active ? '#76B900' : 'var(--border)',
                  color: active ? '#76B900' : 'var(--text-secondary)',
                  background: active ? 'rgba(118,185,0,0.10)' : undefined,
                }}
              >
                {choice}
              </button>
            );
          })}
        </div>
      )}
      {pending && freeText && (
        <textarea
          value={typed}
          onChange={e => setTyped(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={2}
          aria-label="Your answer"
          placeholder={choices.length > 0 ? 'Or type your own answer' : 'Type your answer'}
          className="mt-2 w-full resize-y rounded-sm border px-2 py-1 font-mono text-[11px] outline-none focus:border-[#76B900]"
          style={{ background: 'var(--bg-card)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
        />
      )}
      {pending && overCap && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: '#d97706' }} role="note">
          {answer.length} characters — your message goes through whole; the summary that closes this
          card in history keeps the first {WIZARD_TOOL_OUTCOME_CHARS}.
        </div>
      )}
      {pending && onAnswer === null && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }} role="note">
          Answer this question in the Wizard thread.
        </div>
      )}
      {pending && (
        <div className="mt-2 flex items-center justify-end gap-1">
          <button
            type="button"
            onClick={onDismiss}
            className="rounded-sm border px-2 py-1 text-[10px] font-mono transition-colors hover:bg-[var(--bg-hover)]"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
            title="Leave the question unanswered"
          >
            Skip
          </button>
          {offered && (
            <button
              type="button"
              onClick={submit}
              disabled={!answer || busy}
              className="btn-primary px-2 py-1 text-[10px] font-mono disabled:cursor-not-allowed disabled:opacity-50"
              title={busy ? 'A turn is still streaming — wait for it to finish' : 'Send this answer as your next message'}
            >
              {chrome.runLabel}
            </button>
          )}
        </div>
      )}
      {!pending && result && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-secondary)' }}>
          {status === 'answered' ? `Your answer: ${result}` : result}
        </div>
      )}
    </div>
  );
}

/**
 * The inline render of a confirmed `generate_image`. The outputs route is
 * behind `require_auth`, so a bare `<img src>` would get a 401: the bytes
 * are fetched with the same headers every API call sends (`fetchApiBlob`)
 * and shown from an object URL that is revoked when the card unmounts or
 * the result changes. Under it: backend · model · size · the three-state
 * cost pill, the saved path, and the server's note (when it adds anything
 * the path line does not already say).
 */
function GeneratedImage({ image, alt }: { image: WizardImageResult; alt: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    fetchApiBlob(image.url)
      .then(blob => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(err => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'could not load the image');
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [image.url]);

  const pill = costPill(image.cost_usd, image.cost_known, formatCardCost);
  const noteAddsSomething = typeof image.note === 'string' && image.note.trim() && !image.note.includes(image.path);

  return (
    <div className="mt-2">
      {src ? (
        // An object URL built from bytes fetched with the API headers —
        // next/image has nothing to optimise here.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt={alt}
          className="max-h-72 max-w-full rounded-sm border"
          style={{ borderColor: 'var(--border)' }}
        />
      ) : loadError ? (
        <div className="font-mono text-[10px]" style={{ color: '#dc2626' }} role="note">
          could not load the image: {loadError}
        </div>
      ) : (
        <div className="font-mono text-[10px] italic" style={{ color: 'var(--text-muted)' }} role="status">
          loading the image…
        </div>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-x-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
        <span>{image.backend}</span>
        {image.model && <span>· {image.model}</span>}
        {image.width > 0 && image.height > 0 && <span>· {image.width}×{image.height}</span>}
        {pill && (
          <span data-cost-state={pill.state} title={pill.title} style={pill.title ? { cursor: 'help' } : undefined}>
            · {pill.text}
          </span>
        )}
      </div>
      <div className="mt-0.5 break-all font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
        saved to {image.path}
      </div>
      {noteAddsSomething && (
        <div className="mt-0.5 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
          {image.note}
        </div>
      )}
    </div>
  );
}

/**
 * One line for a tool's successful outcome — the card's result row and the
 * `✓ <tool> — …` system line the thread appends. Shape-aware for the tools
 * whose answers carry no `summary` (the 0.46 image, vault and question
 * results among them); anything else is a short JSON excerpt.
 */
export function formatToolResultSummary(name: string, result: Record<string, unknown> | undefined): string {
  if (!result) return 'completed.';
  if (typeof result.summary === 'string') return result.summary;
  // Specific shapes we recognize for prettier surface text:
  const image = imageResult(result);
  if (image) {
    const by = [image.backend, image.model].filter(part => typeof part === 'string' && part).join(' · ');
    return `image saved to ${image.path}${by ? ` (${by})` : ''}`;
  }
  const saved = rememberResult(result);
  if (saved) {
    return `saved to your vault: ${saved.title || 'note'}${saved.category ? ` (${saved.category})` : ''}`;
  }
  if (result.answered_in_chat === true) {
    return 'answered in the chat — nothing ran on the server';
  }
  if (name === 'validate_provider_key' && typeof result.valid === 'boolean') {
    if (result.valid) {
      const count = typeof result.model_count === 'number' ? `, ${result.model_count} model(s)` : '';
      const latency = typeof result.latency_ms === 'number' ? ` in ${result.latency_ms} ms` : '';
      return `valid${latency}${count}`;
    }
    return `invalid: ${result.error ?? 'unknown'}`;
  }
  if (name === 'save_provider_key' && result.ok === true) {
    return `saved under ${result.config_file ?? 'workspace config'}`;
  }
  // Fall through: stringify a short version.
  try {
    return JSON.stringify(result).slice(0, 120);
  } catch {
    return 'completed.';
  }
}
