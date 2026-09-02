/**
 * Shared conversation → Markdown-download export for every sidebar's
 * "Export" menu item. Wizard-meta tails are stripped so the export contains
 * prose, not machine JSON.
 */

import { exportConversationMarkdown } from '@/components/ChatMessage';
import { getConversation } from './api';
import type { ChatMessage } from './types';
import { parseWizardMeta } from './wizardMeta';

export function downloadMarkdown(markdown: string, title: string): void {
  const slug =
    title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48) ||
    'conversation';
  const blob = new Blob([markdown], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${slug}.md`;
  a.click();
  URL.revokeObjectURL(url);
}

export function advisorLabel(msgs: ChatMessage[]): string {
  const lastAssistant = msgs.slice().reverse().find(m => m.role === 'assistant');
  return lastAssistant?.model
    ? `${lastAssistant.model}${lastAssistant.provider ? ` (${lastAssistant.provider})` : ''}`
    : lastAssistant?.provider ?? 'assistant';
}

/** Export a conversation by id. Returns true if a download was produced. */
export async function exportConversationById(id: string): Promise<boolean> {
  const detail = await getConversation(id);
  if (!detail?.messages?.length) return false;
  const msgs: ChatMessage[] = detail.messages
    .filter(m => m.role !== 'system')
    .map(m => ({
      id: m.id,
      role: (m.role === 'user' ? 'user' : 'assistant') as 'user' | 'assistant',
      content: m.role === 'assistant' ? parseWizardMeta(m.content).text : m.content,
      provider: m.provider,
      model: m.model,
      timestamp: m.timestamp,
    }));
  downloadMarkdown(
    exportConversationMarkdown(msgs, advisorLabel(msgs), new Date().toISOString().slice(0, 10)),
    detail.title || 'conversation'
  );
  return true;
}
