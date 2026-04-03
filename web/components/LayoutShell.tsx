'use client';

import { usePathname, useRouter } from 'next/navigation';
import Sidebar from '@/components/Sidebar';
import GlobalModals from '@/components/GlobalModals';
import { UIShellProvider, useUIShell } from '@/components/UIShellProvider';

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
  const { openCommandPalette } = useUIShell();

  if (isChatPage) {
    // Chat page is fully self-contained — it renders its own sidebar.
    return (
      <>
        <GlobalModals />
        {children}
      </>
    );
  }

  return (
    <>
      <GlobalModals />
      {/* Top status bar */}
      <div className="fixed top-0 left-0 right-0 z-50 h-8 bg-[#111111] border-b border-[#333333] flex items-center px-4 gap-6 text-[10px] font-mono text-[#666666]">
        <span className="text-[#76B900] font-bold tracking-widest uppercase">COUNCIL</span>
        <span className="text-[#444444]">|</span>
        <span>AI Command Center</span>
        <span className="text-[#444444]">|</span>
        <span className="text-[#76B900]">NVIDIA Nemotron Ready</span>
        <div className="ml-auto flex items-center gap-4">
          {/* Command palette trigger */}
          <button
            className="text-[#333333] hover:text-[#76B900] font-mono text-[10px] transition-colors"
            title="Open command palette (Ctrl+K)"
            onClick={openCommandPalette}
          >
            Ctrl+K
          </button>
          <span className="text-[#444444]">v0.3.0</span>
        </div>
      </div>
      {/* Offset for top bar */}
      <div className="pt-8 layout-with-sidebar">
        <Sidebar onNewChat={() => router.push('/')} />
        <main className="flex-1 min-w-0 overflow-auto">
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
