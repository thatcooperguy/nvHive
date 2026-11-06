'use client';

/**
 * /wizard — a one-release redirect to the chat home (0.47 S2, SIMPLIFY
 * design §3). The Wizard is now the default mode of `/`; this page only
 * carries old links there, renaming `?conversation=<id>` to `?c=<id>` and
 * keeping every other parameter (`starter`, `profile`, `issue`). It retires
 * with the `/query` and `/council` redirects (O14).
 */

import { useRouter } from 'next/navigation';
import { useEffect } from 'react';

export default function WizardRedirect() {
  const router = useRouter();
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const id = params.get('conversation');
    if (id) {
      params.delete('conversation');
      params.set('c', id);
    }
    const query = params.toString();
    router.replace(query ? `/?${query}` : '/');
  }, [router]);
  return null;
}
