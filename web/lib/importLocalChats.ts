/**
 * One-time import of the pre-0.42 browser-local chat store
 * ('council_chats_v2') into the server-side conversations store.
 *
 * Runs on first load after upgrade. Each thread goes up as ONE request
 * (conversation + turns + pinned flag, stored in one transaction) and leaves
 * the local store the moment it lands, so a failure part-way keeps exactly
 * the unimported threads for the next load — never a duplicate, never a
 * half-thread. The key is removed once the store is empty.
 */

import { createConversation, type AppendMessageInput } from './api';
import { messageText } from './exportConversation';
import type { ChatMessage, ConversationSummary } from './types';

export const LEGACY_CHATS_KEY = 'council_chats_v2';

export function hasLegacyChats(): boolean {
  try {
    return typeof window !== 'undefined' && localStorage.getItem(LEGACY_CHATS_KEY) !== null;
  } catch {
    return false;
  }
}

interface LegacyStore {
  conversations: ConversationSummary[];
  messages: Record<string, ChatMessage[]>;
}

function readLegacyStore(): LegacyStore | null {
  try {
    const raw = localStorage.getItem(LEGACY_CHATS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LegacyStore>;
    return {
      conversations: Array.isArray(parsed.conversations) ? parsed.conversations : [],
      messages: parsed.messages && typeof parsed.messages === 'object' ? parsed.messages : {},
    };
  } catch {
    return null;
  }
}

function writeLegacyStore(store: LegacyStore): void {
  try {
    if (store.conversations.length === 0) localStorage.removeItem(LEGACY_CHATS_KEY);
    else localStorage.setItem(LEGACY_CHATS_KEY, JSON.stringify(store));
  } catch {
    // Storage write refused: the thread is already on the server, and the
    // next load will see it in existingIds only if the id matched — worst
    // case is one retry, not data loss.
  }
}

/** Forget one thread (imported or skipped) so a retry resumes after it. */
function dropThread(store: LegacyStore, id: string): void {
  store.conversations = store.conversations.filter(c => c.id !== id);
  delete store.messages[id];
  writeLegacyStore(store);
}

/** Flatten a thread with the same helper the chat page persists with, so
 * compare/council replies whose `content` is empty import as Markdown
 * instead of being skipped. Error rows and blank turns are dropped. */
function wireMessages(msgs: ChatMessage[]): AppendMessageInput[] {
  const out: AppendMessageInput[] = [];
  for (const msg of msgs) {
    if (msg.role === 'error') continue;
    const content = messageText(msg);
    if (!content.trim()) continue;
    out.push({
      role: msg.role,
      content,
      provider: msg.provider,
      model: msg.model,
      tokens: msg.tokens,
      cost_usd: msg.cost_usd,
      latency_ms: msg.latency_ms,
    });
  }
  return out;
}

/**
 * Import every stored thread with at least one message, oldest first.
 * Returns the number of conversations imported this pass. A thread that
 * fails to import stays in the store (with everything after it) so the
 * next load resumes from there.
 */
export async function importLegacyChats(existingIds: Set<string>): Promise<number> {
  if (typeof window === 'undefined') return 0;
  const store = readLegacyStore();
  if (!store) {
    // Unparseable leftovers are not worth a retry loop.
    if (localStorage.getItem(LEGACY_CHATS_KEY) !== null) localStorage.removeItem(LEGACY_CHATS_KEY);
    return 0;
  }

  // Oldest first so server updated_at ordering roughly follows the original.
  const threads = [...store.conversations].sort(
    (a, b) => (a.updated_at ?? 0) - (b.updated_at ?? 0)
  );

  let imported = 0;
  for (const conv of threads) {
    const messages = existingIds.has(conv.id) ? [] : wireMessages(store.messages[conv.id] ?? []);
    if (messages.length > 0) {
      const created = await createConversation(
        conv.title || '',
        conv.mode === 'wizard' ? 'single' : conv.mode,
        { pinned: Boolean(conv.pinned), messages }
      );
      if (!created) return imported;
      imported += 1;
    }
    dropThread(store, conv.id);
  }
  writeLegacyStore(store);
  return imported;
}
