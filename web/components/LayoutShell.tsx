'use client';

import { usePathname, useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
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
  CONVERSATIONS_CHANGED_EVENT,
  announceConversationsChanged,
  deleteConversation,
  getConversations,
  pinConversation,
  renameConversation,
} from '@/lib/api';
import { exportConversationById } from '@/lib/exportConversation';
import { hasLegacyChats, importLegacyChats } from '@/lib/importLocalChats';
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

  // Chat history for the shared sidebar. One store: the server. Refreshed on
  // navigation and whenever a component announces a change (e.g. a wizard
  // turn just created a conversation on this very page).
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);

  const refreshConversations = useCallback(() => {
    getConversations().then(setConversations).catch(() => {});
  }, []);

  // Pre-0.42 builds kept main-page chats in localStorage. Import them into
  // the server store once, on whichever page loads first after the upgrade.
  const importStartedRef = useRef(false);
  useEffect(() => {
    if (importStartedRef.current || !hasLegacyChats()) return;
    importStartedRef.current = true;
    getConversations()
      .then(list => importLegacyChats(new Set(list.map(c => c.id))))
      .then(imported => { if (imported > 0) announceConversationsChanged(); })
      .catch(() => {});
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
    window.addEventListener(CONVERSATIONS_CHANGED_EVENT, refreshConversations);
    return () => window.removeEventListener(CONVERSATIONS_CHANGED_EVENT, refreshConversations);
  }, [pathname, isChatPage, isSetupPage, refreshConversations]);

  // A conversation resumes on the surface that produced it.
  const handleSelectConversation = useCallback((id: string) => {
    if (!id) {
      router.push('/');
      return;
    }
    const conv = conversations.find(c => c.id === id);
    router.push(
      conv?.mode === 'wizard'
        ? `/wizard?conversation=${encodeURIComponent(id)}`
        : `/?c=${encodeURIComponent(id)}`
    );
  }, [conversations, router]);

  const handleRenameConversation = useCallback((id: string, title: string) => {
    setConversations(prev => prev.map(c => c.id === id ? { ...c, title } : c));
    void renameConversation(id, title);
  }, []);

  const handleDeleteConversation = useCallback((id: string) => {
    setConversations(prev => prev.filter(c => c.id !== id));
    void deleteConversation(id);
  }, []);

  const handlePinConversation = useCallback((id: string) => {
    const nextPinned = !conversations.find(c => c.id === id)?.pinned;
    setConversations(prev => prev.map(c => c.id === id ? { ...c, pinned: nextPinned } : c));
    void pinConversation(id, nextPinned);
  }, [conversations]);

  const handleExportConversation = useCallback((id: string) => {
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
            exposes [Restart API]/[Deep status] bridges that spawn the rootless
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
