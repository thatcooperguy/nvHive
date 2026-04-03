'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import CommandPalette from '@/components/CommandPalette';
import ShortcutHelp from '@/components/ShortcutHelp';
import KeyboardShortcuts from '@/components/KeyboardShortcuts';
import { useUIShell } from '@/components/UIShellProvider';
import { getModels } from '@/lib/api';

interface ModelOption {
  model_id: string;
  display_name: string;
  provider: string;
}

/**
 * GlobalModals — renders the command palette, shortcut help overlay, and the
 * global keyboard shortcut listener.  Mount this once inside LayoutShell (or
 * any shared root component) so it is present on every page.
 */
export default function GlobalModals() {
  const { modal, closeModal, dispatchShortcut } = useUIShell();
  const router = useRouter();
  const [models, setModels] = useState<ModelOption[]>([]);

  // Pre-fetch models for the palette model-switch actions
  useEffect(() => {
    getModels()
      .then(data => setModels(data.models.map(m => ({
        model_id: m.model_id,
        display_name: m.display_name,
        provider: m.provider,
      }))))
      .catch(() => {});
  }, []);

  function handlePaletteAction(id: string) {
    switch (id) {
      case 'new-chat':
        dispatchShortcut('new-chat');
        router.push('/');
        break;
      case 'search-conversations':
        dispatchShortcut('new-chat'); // navigate home; search is in sidebar
        router.push('/');
        break;
      case 'mode-ask':
        dispatchShortcut('mode-ask');
        router.push('/');
        break;
      case 'mode-convene':
        dispatchShortcut('mode-convene');
        router.push('/');
        break;
      case 'mode-poll':
        dispatchShortcut('mode-poll');
        router.push('/');
        break;
      case 'mode-throwdown':
        dispatchShortcut('mode-convene'); // maps to council
        router.push('/');
        break;
      case 'nav-system':
        router.push('/system');
        break;
      case 'nav-providers':
        router.push('/providers');
        break;
      case 'nav-integrations':
        router.push('/integrations');
        break;
      case 'nav-analytics':
        router.push('/analytics');
        break;
      case 'nav-settings':
        router.push('/settings');
        break;
      case 'toggle-sidebar':
        dispatchShortcut('toggle-sidebar');
        break;
      case 'show-shortcuts':
        closeModal();
        // slight delay so palette closes first
        setTimeout(() => dispatchShortcut('show-shortcuts'), 80);
        break;
      default:
        // model-switch actions
        if (id.startsWith('model-switch:') || id.startsWith('model-')) {
          const modelId = id.replace('model-switch:', '');
          // Publish via custom event so page.tsx can listen
          window.dispatchEvent(new CustomEvent('hive:switch-model', { detail: { modelId } }));
          router.push('/');
        }
        break;
    }
  }

  return (
    <>
      <KeyboardShortcuts onAction={dispatchShortcut} />
      <CommandPalette
        open={modal === 'command-palette'}
        onClose={closeModal}
        onAction={handlePaletteAction}
        models={models}
      />
      <ShortcutHelp
        open={modal === 'shortcut-help'}
        onClose={closeModal}
      />
    </>
  );
}
