'use client';

import { usePathname, useRouter } from 'next/navigation';
import Sidebar from '@/components/Sidebar';
import GlobalModals from '@/components/GlobalModals';
import ThemeToggle from '@/components/ThemeToggle';
import WelcomeBackPanel from '@/components/WelcomeBackPanel';
import { UIShellProvider, useUIShell } from '@/components/UIShellProvider';
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

  if (isChatPage || isSetupPage) {
    // Chat and setup are self-contained surfaces — but they still want the
    // theme toggle, so render it in a fixed corner overlay.
    // The chat page already includes its own WelcomeBackPanel inline, so we
    // don't double-mount it here.
    return (
      <>
        <GlobalModals />
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
      {/* Offset for top bar */}
      <div className="pt-8 layout-with-sidebar">
        <Sidebar onNewChat={() => router.push('/')} />
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
