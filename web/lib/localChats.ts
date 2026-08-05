/**
 * Browser-local chat store ('council_chats_v2').
 *
 * The main chat page keeps single/council/compare threads in localStorage
 * (offline-friendly; they are not persisted server-side yet). The shared
 * sidebar in LayoutShell merges these into its list so main-page history is
 * browsable from every page, and writes rename/pin/delete back here for
 * local-only conversations. The chat page re-reads on mount, so mutations
 * made elsewhere are picked up on navigation.
 */

import type { ChatMessage, ConversationSummary } from './types';

export const LOCAL_CHATS_KEY = 'council_chats_v2';

export interface StoredChatState {
  conversations: ConversationSummary[];
  messages: Record<string, ChatMessage[]>;
}

const EMPTY: StoredChatState = { conversations: [], messages: {} };

export function readStoredChats(): StoredChatState {
  if (typeof window === 'undefined') return EMPTY;
  try {
    const parsed = JSON.parse(
      localStorage.getItem(LOCAL_CHATS_KEY) ?? '{"conversations":[],"messages":{}}'
    ) as StoredChatState;
    return {
      conversations: Array.isArray(parsed.conversations) ? parsed.conversations : [],
      messages: parsed.messages && typeof parsed.messages === 'object' ? parsed.messages : {},
    };
  } catch {
    return EMPTY;
  }
}

export function mutateStoredChats(
  mutator: (prev: StoredChatState) => StoredChatState
): StoredChatState {
  const next = mutator(readStoredChats());
  if (typeof window !== 'undefined') {
    try {
      localStorage.setItem(LOCAL_CHATS_KEY, JSON.stringify(next));
    } catch {
      // Quota errors — keep the in-memory result.
    }
  }
  return next;
}

/** Event fired whenever a conversation is created/renamed/pinned/deleted so
 * mounted sidebars can refresh without a navigation. */
export const CONVERSATIONS_CHANGED_EVENT = 'nvh:conversations-changed';

export function announceConversationsChanged(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(CONVERSATIONS_CHANGED_EVENT));
  }
}
