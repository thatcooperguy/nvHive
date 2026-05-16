'use client';

/**
 * /wizard — dedicated AI Wizard chat surface.
 *
 * Wraps the WizardChat component with a header so it has its own room. The
 * HardwareWidgetHero pins live workspace state to the top so the user can
 * see what the Wizard sees while they chat.
 */

import { useRouter } from 'next/navigation';
import { Suspense } from 'react';
import GPURecommenderCard from '@/components/GPURecommenderCard';
import WizardChat from '@/components/WizardChat';

export default function WizardPage() {
  const router = useRouter();
  return (
    <div className="flex h-[calc(100vh-2rem)] flex-col">
      <div
        className="border-b px-4 py-3"
        style={{ borderColor: 'var(--border)', background: 'var(--bg-secondary)' }}
      >
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[#76B900]" />
          <span className="text-[10px] font-mono font-bold uppercase tracking-[0.18em]" style={{ color: '#76B900' }}>
            AI Wizard
          </span>
        </div>
        <div className="mt-1 text-base font-semibold tracking-tight" style={{ color: 'var(--text-primary)' }}>
          Your in-product guide for nvHive
        </div>
        <div className="mt-0.5 text-xs" style={{ color: 'var(--text-secondary)' }}>
          Reads your live GPU, persistent storage, providers, jobs, receipts,
          and vault. Can refresh models, run safe repairs, and validate
          provider keys — confirm-class actions always show a button first.
        </div>
        {/* First-5-minute nudge: turn raw VRAM into a single Install CTA. */}
        <GPURecommenderCard onInstall={() => router.push('/setup')} />
      </div>
      <div className="flex-1 min-h-0">
        {/* Suspense boundary required because WizardChat uses
            useSearchParams() (to read the ?issue=… / ?starter=… deep-link
            from the setup page). Next.js 16's static prerender errors out
            without one. The fallback is invisible — the chat loads in
            <50ms so users never see a flash. */}
        <Suspense fallback={null}>
          <WizardChat />
        </Suspense>
      </div>
    </div>
  );
}
