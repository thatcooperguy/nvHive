/**
 * One-time import of the pre-0.42 browser-local chat store
 * ('council_chats_v2') into the server-side conversations store.
 *
 * Runs on first load after upgrade, then deletes the key. Idempotent and
 * retry-safe: any server failure aborts before the key is removed, so the
 * next load tries again; the partially-created conversation is deleted so
 * retries never leave empty duplicates behind.
 */

import {
  appendConversationMessage,
  createConversation,
  deleteConversation,
  pinConversation,
} from './api';
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

function compareMarkdown(msg: ChatMessage): string {
  if (!msg.compare_data) return msg.content;
  return Object.entries(msg.compare_data)
    .map(([provider, r]) => `### ${provider} (${r.model})\n\n${r.content}`)
    .join('\n\n');
}

/**
 * Import every stored thread with at least one message. Returns the number
 * of conversations imported, or -1 when the import was aborted (server
 * unreachable or the append endpoint missing) and should be retried later.
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
  const threads = store.conversations
    .filter(c => !existingIds.has(c.id) && (store.messages[c.id]?.length ?? 0) > 0)
    .sort((a, b) => (a.updated_at ?? 0) - (b.updated_at ?? 0));

  let imported = 0;
  for (const conv of threads) {
    const created = await createConversation(conv.title || '', conv.mode === 'wizard' ? 'single' : conv.mode);
    if (!created) return -1;
    for (const msg of store.messages[conv.id]) {
      if (msg.role === 'error') continue;
      const content = msg.role === 'assistant' && msg.compare_data ? compareMarkdown(msg) : msg.content;
      if (!content.trim()) continue;
      const ok = await appendConversationMessage(created.id, {
        role: msg.role,
        content,
        provider: msg.provider,
        model: msg.model,
        tokens: msg.tokens,
        cost_usd: msg.cost_usd,
        latency_ms: msg.latency_ms,
      });
      if (!ok) {
        await deleteConversation(created.id);
        return -1;
      }
    }
    if (conv.pinned) await pinConversation(created.id, true);
    imported += 1;
  }

  localStorage.removeItem(LEGACY_CHATS_KEY);
  return imported;
}
