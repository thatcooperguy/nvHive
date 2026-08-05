'use client';

import { usePathname, useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import ApiHealthBanner from '@/components/ApiHealthBanner';
import DebugReportButton from '@/components/DebugReportButton';
import Sidebar from '@/components/Sidebar';
import GlobalModals from '@/components/GlobalModals';
import SessionAgePill from '@/components/SessionAgePill';
import SystemConsole from '@/components/SystemConsole';
import ThemeToggle from '@/components/ThemeToggle';
import WelcomeBackPanel from '@/components/WelcomeBackPanel';
import { UIShellProvider, useUIShell } from '@/components/UIShellProvider';
import {
  deleteConversation,
  getConversations,
  pinConversation,
  renameConversation,
} from '@/lib/api';
import { exportConversationById } from '@/lib/exportConversation';
import {
  CONVERSATIONS_CHANGED_EVENT,
  mutateStoredChats,
  readStoredChats,
} from '@/lib/localChats';
import type { ConversationSummary } from '@/lib/types';
import { NVHIVE_VERSION } from '@/lib/version';

/**
 * LayoutShell wraps every page.
 *
 * - The root `/` route is the chat app — it manages its OWN full-screen layout
 *   with its own sidebar, so we render children directly (no wrapper).
 * - All other routes get the classic top-bar + sidebar shell.
 *
 * UIShellProvider + GlobalModals are mounted in both branches so keyboard
 * shortcuts and the command palette work on every page.
 */

/** Inner shell — needs access to UIShell context, so must be inside the provider */
function InnerShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const isChatPage = pathname === '/';
  const isSetupPage = pathname?.startsWith('/setup');
  const { openCommandPalette } = useUIShell();

  // Chat history for the shared sidebar, so past conversations are browsable
  // from every page — not just the root chat. Two sources merged: the server
  // (wizard chats + anything persisted) and the browser-local store (main-page
  // single/council chats, which stay client-side). Refreshed on navigation
  // and whenever a component announces a change (e.g. a wizard turn just
  // created a conversation on this very page).
  const [serverConvs, setServerConvs] = useState<ConversationSummary[]>([]);
  const [localConvs, setLocalConvs] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);

  const refreshConversations = useCallback(() => {
    getConversations().then(setServerConvs).catch(() => {});
    setLocalConvs(readStoredChats().conversations);
  }, []);

  useEffect(() => {
    if (isChatPage || isSetupPage) return; // those surfaces manage their own
    refreshConversations();
    // Highlight the open conversation (both surfaces carry it in the URL:
    // /wizard?conversation=<id>, /?c=<id>). usePathname excludes the query,
    // so read it off location once navigation has committed.
    try {
      const params = new URLSearchParams(window.location.search);
      setActiveId(params.get('conversation') ?? params.get('c'));
    } catch {
      setActiveId(null);
    }
    const onChanged = () => refreshConversations();
    window.addEventListener(CONVERSATIONS_CHANGED_EVENT, onChanged);
    return () => window.removeEventListener(CONVERSATIONS_CHANGED_EVENT, onChanged);
  }, [pathname, isChatPage, isSetupPage, refreshConversations]);

  // Merge: server metadata wins for ids it knows; local-only chats are kept.
  const conversations: ConversationSummary[] = (() => {
    const byId = new Map<string, ConversationSummary>();
    for (const c of localConvs) byId.set(c.id, c);
    for (const c of serverConvs) {
      const local = byId.get(c.id);
      byId.set(c.id, { ...local, ...c, pinned: c.pinned || local?.pinned });
    }
    return [...byId.values()].sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
  })();

  const isServerConv = useCallback(
    (id: string) => serverConvs.some(c => c.id === id),
    [serverConvs],
  );

  // A conversation resumes on the surface that produced it.
  const handleSelectConversation = useCallback((id: string) => {
    if (!id) {
      router.push('/');
      return;
    }
    const conv = conversations.find(c => c.id === id);
    if (conv?.mode === 'wizard') {
      router.push(`/wizard?conversation=${encodeURIComponent(id)}`);
    } else {
      router.push(`/?c=${encodeURIComponent(id)}`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverConvs, localConvs, router]);

  // Rename/delete/pin: server-known conversations sync to the API; local-only
  // ones write through to the browser store the chat page reads on mount.
  const handleRenameConversation = useCallback((id: string, title: string) => {
    setServerConvs(prev => prev.map(c => c.id === id ? { ...c, title } : c));
    setLocalConvs(prev => prev.map(c => c.id === id ? { ...c, title } : c));
    if (isServerConv(id)) {
      void renameConversation(id, title);
    } else {
      mutateStoredChats(prev => ({
        ...prev,
        conversations: prev.conversations.map(c => c.id === id ? { ...c, title } : c),
      }));
    }
  }, [isServerConv]);

  const handleDeleteConversation = useCallback((id: string) => {
    setServerConvs(prev => prev.filter(c => c.id !== id));
    setLocalConvs(prev => prev.filter(c => c.id !== id));
    if (isServerConv(id)) {
      void deleteConversation(id);
    } else {
      mutateStoredChats(prev => ({
        conversations: prev.conversations.filter(c => c.id !== id),
        messages: Object.fromEntries(
          Object.entries(prev.messages).filter(([k]) => k !== id)
        ),
      }));
    }
  }, [isServerConv]);

  const handlePinConversation = useCallback((id: string) => {
    const current = conversations.find(c => c.id === id);
    const nextPinned = !current?.pinned;
    setServerConvs(prev => prev.map(c => c.id === id ? { ...c, pinned: nextPinned } : c));
    setLocalConvs(prev => prev.map(c => c.id === id ? { ...c, pinned: nextPinned } : c));
    if (isServerConv(id)) {
      void pinConversation(id, nextPinned);
    } else {
      mutateStoredChats(prev => ({
        ...prev,
        conversations: prev.conversations.map(c => c.id === id ? { ...c, pinned: nextPinned } : c),
      }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverConvs, localConvs, isServerConv]);

  const handleExportConversation = useCallback((id: string) => {
    // Shared helper: server copy first (meta tails stripped), local fallback.
    void exportConversationById(id);
  }, []);

  if (isChatPage || isSetupPage) {
    // Chat and setup are self-contained surfaces — but they still want the
    // theme toggle, so render it in a fixed corner overlay.
    // The chat page already includes its own WelcomeBackPanel inline, so we
    // don't double-mount it here.
    return (
      <>
        <GlobalModals />
        {/* The SystemConsole is the load-bearing fix for "the WebUI says
            run `nvh serve` in a terminal." It tails $NVH_HOME/logs/*.log
            via a Next.js route (so it works when FastAPI is dead) and
            exposes [Restart API]/[Doctor] bridges that spawn the rootless
            CLI directly. Surfaced on every page including chat + setup
            so a fresh-install user never has to leave the browser. */}
        <SystemConsole />
        {/* Surfaces "API offline" on the chat + setup pages too — these
            are the first pages a fresh-install user lands on, and they
            both depend on the API for everything they render. */}
        <ApiHealthBanner />
        {/* One-click "show me everything" diagnostic the user can
            photograph + share. Sits bottom-left so it's reachable on
            every page including chat + setup. */}
        <DebugReportButton />
        <div className="pointer-events-auto fixed right-3 top-3 z-50">
          <ThemeToggle compact />
        </div>
        {children}
      </>
    );
  }

  return (
    <>
      <GlobalModals />
      <SystemConsole />
      <ApiHealthBanner />
      <DebugReportButton />
      {/* Top status bar */}
      <div
        className="fixed top-0 left-0 right-0 z-50 h-8 border-b flex items-center px-4 gap-6 text-[10px] font-mono"
        style={{
          background: 'var(--bg-primary)',
          borderColor: 'var(--border)',
          color: 'var(--text-muted)',
        }}
      >
        <span className="text-[#76B900] font-bold tracking-widest uppercase">NVHIVE</span>
        <span style={{ color: 'var(--border-bright)' }}>|</span>
        <span>Rootless NVIDIA AI Workspace</span>
        <span style={{ color: 'var(--border-bright)' }}>|</span>
        <span className="text-[#5a9100]">Local-first mode</span>
        <div className="ml-auto flex items-center gap-3">
          {/* Session-age indicator — load-bearing for metered cloud-GPU users. */}
          <SessionAgePill />
          <span style={{ color: 'var(--border-bright)' }}>|</span>
          {/* Command palette trigger */}
          <button
            className="font-mono text-[10px] transition-colors hover:text-[#76B900]"
            style={{ color: 'var(--text-muted)' }}
            title="Open command palette (Ctrl+K)"
            onClick={openCommandPalette}
          >
            Ctrl+K
          </button>
          <ThemeToggle compact />
          <span style={{ color: 'var(--text-faint)' }}>v{NVHIVE_VERSION}</span>
        </div>
      </div>
      {/* Offset for top bar (32px) + SystemConsole collapsed bar (24px) = 56px */}
      <div className="pt-14 layout-with-sidebar">
        <Sidebar
          onNewChat={() => router.push('/')}
          conversations={conversations}
          activeConversationId={activeId}
          onSelectConversation={handleSelectConversation}
          onRenameConversation={handleRenameConversation}
          onDeleteConversation={handleDeleteConversation}
          onPinConversation={handlePinConversation}
          onExportConversation={handleExportConversation}
        />
        <main className="flex-1 min-w-0 overflow-auto">
          {/* Reconnect awareness — render on /vault, /wizard, /settings, etc.
              so users coming back to a non-chat surface still see what changed
              from last session. Per-tab dismissal lives in the panel. */}
          <div className="mx-auto max-w-5xl px-4 pt-4">
            <WelcomeBackPanel />
          </div>
          {children}
        </main>
      </div>
    </>
  );
}

export default function LayoutShell({ children }: { children: React.ReactNode }) {
  return (
    <UIShellProvider>
      <InnerShell>{children}</InnerShell>
    </UIShellProvider>
  );
}
