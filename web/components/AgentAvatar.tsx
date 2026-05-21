'use client';

/**
 * AgentAvatar — renders a profile's avatar in three sizes. Falls back to an
 * initial monogram on a deterministic color when the avatar fails to load
 * (e.g. user profile with no uploaded image yet, or a stale URL).
 *
 * Used by the chat composer picker, the chat message bubble, and the (future)
 * create-agent form preview.
 */

import { useState } from 'react';
import { profileAvatarUrl, type AgentProfileSchema } from '@/lib/api';

type Size = 'sm' | 'md' | 'lg';

const SIZE_PX: Record<Size, number> = { sm: 20, md: 28, lg: 56 };

// Stable color from the profile name so different agents look different in
// the fallback path. Hashing 6 characters of name → 6 distinct hues is
// enough; tighter clusters wouldn't be readable anyway.
const FALLBACK_HUES = [
  '#76B900', '#0ea5e9', '#a855f7', '#f59e0b', '#dc2626', '#10b981',
];

function fallbackColor(name: string): string {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return FALLBACK_HUES[h % FALLBACK_HUES.length];
}

export default function AgentAvatar({
  profile,
  size = 'md',
}: {
  profile: AgentProfileSchema | null | undefined;
  size?: Size;
}) {
  const [errored, setErrored] = useState(false);
  const px = SIZE_PX[size];
  const url = !errored ? profileAvatarUrl(profile) : null;
  const name = profile?.title ?? profile?.name ?? '?';
  const initial = name.trim().charAt(0).toUpperCase() || '?';

  if (url) {
    return (
      <img
        src={url}
        alt={`${name} avatar`}
        width={px}
        height={px}
        loading="lazy"
        onError={() => setErrored(true)}
        className="flex-shrink-0 rounded-md object-cover"
        style={{ background: 'var(--bg-subtle)' }}
      />
    );
  }

  // Fallback monogram. Mirrors the built-in SVG shape (rounded square) so
  // the picker has a stable silhouette even before the image loads.
  return (
    <div
      style={{
        width: px,
        height: px,
        background: fallbackColor(profile?.name ?? ''),
        color: '#ffffff',
        fontSize: Math.floor(px * 0.45),
      }}
      className="flex flex-shrink-0 items-center justify-center rounded-md font-mono font-bold"
      aria-label={`${name} avatar`}
    >
      {initial}
    </div>
  );
}
