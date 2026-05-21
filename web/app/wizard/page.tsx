'use client';

/**
 * /wizard — dedicated AI Wizard chat surface.
 *
 * Wraps the WizardChat component with the shared PageHeader so the
 * eyebrow / title / subtitle styling matches every other route.
 */

import { useRouter } from 'next/navigation';
import { Suspense } from 'react';
import GPURecommenderCard from '@/components/GPURecommenderCard';
import PageHeader from '@/components/PageHeader';
import WizardChat from '@/components/WizardChat';

export default function WizardPage() {
  const router = useRouter();
  return (
    <div className="flex h-[calc(100vh-2rem)] flex-col">
      <PageHeader
        eyebrow="AI Wizard"
        title="Your in-product guide for nvHive"
        subtitle="Reads your live GPU, persistent storage, providers, jobs, receipts, and vault. Can refresh models, run safe repairs, and validate provider keys — confirm-class actions always show a button first."
      />
      <div className="mx-auto w-full max-w-5xl px-4 pt-3">
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
