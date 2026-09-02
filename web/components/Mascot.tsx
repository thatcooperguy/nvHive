'use client';

/**
 * Mascot — the always-present sprite guide in the bottom-right corner.
 *
 * Reacts to what the Wizard is doing (thinking / working / asking / happy /
 * error / sleeping) via the store in lib/mascot.ts; WizardChat publishes,
 * this component only renders. Art comes from a sprite sheet described by
 * /mascot/manifest.json — bundled at build time (lib/mascot.ts) and
 * re-fetched at mount so an approved likeness can replace the placeholder
 * without a rebuild (docs/MASCOT.md).
 *
 * Rendering: one <span> whose background is the sheet; the current state's
 * row is selected with background-position-y and the frames are walked with
 * a CSS steps() animation over background-position-x (keyframes
 * `mascot-strip` in globals.css, parameterised by CSS variables set here).
 * Remounting the span on state change (key={state}) restarts the animation.
 * prefers-reduced-motion disables the animation → static first frame.
 *
 * Placement: fixed, right 12px, bottom 120px. Bottom-right is otherwise free
 * (DebugReportButton is bottom-left, SystemConsole/ApiHealthBanner hug the
 * top, toasts are centred) but the chat/wizard composers span the full width
 * with their Send button flush right, so the bottom OFFSET — not z-index —
 * is what keeps the sprite clear of them.
 *
 * Layering: z-index 39. That is above normal page content and BELOW every
 * overlay that must be able to cover it: the chat page's mobile sidebar
 * backdrop (z-40) and drawer (z-50), CreateAgentModal / the providers modal
 * / the Sidebar context menu / toasts / the top bar (all z-50), the banners
 * (60, 65), DebugReportButton (80) and its report (110). An open modal or
 * drawer therefore dims and click-blocks the sprite, bubble and menu instead
 * of leaving them floating on top of it.
 *
 * Menu a11y: the menu is rendered AFTER its trigger in DOM order, focus moves
 * to the first item on open, Escape closes and returns focus to the trigger,
 * ArrowUp/Down/Home/End move between items, and the menu closes when focus
 * leaves the widget (focusout) or on a pointer-down outside it.
 */

import { usePathname, useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  MASCOT_STATE_LABELS,
  dismissMascotTip,
  isMascotManifest,
  noteMascotActivity,
  readMascotHidden,
  resolveStrip,
  sayMascotTip,
  setMascotManifest,
  setMascotState,
  useMascotStore,
  writeMascotHidden,
} from '@/lib/mascot';

const MANIFEST_URL = '/mascot/manifest.json';
/** See the "Layering" note above before changing this. */
export const MASCOT_Z_INDEX = 39;
const CORNER: React.CSSProperties = {
  position: 'fixed',
  right: '0.75rem',
  bottom: '7.5rem',
  zIndex: MASCOT_Z_INDEX,
};
const MENU_ID = 'nvh-mascot-menu';

type SpriteStyle = React.CSSProperties & Record<`--${string}`, string | number>;

function menuItems(root: HTMLElement | null): HTMLElement[] {
  return root ? Array.from(root.querySelectorAll<HTMLElement>('[role="menuitem"]')) : [];
}

export default function Mascot() {
  const router = useRouter();
  const pathname = usePathname();
  const { state, tip, manifest } = useMascotStore();
  const [mounted, setMounted] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Hydrate the hidden preference, arm the sleep timer, and load the runtime
  // manifest (a bad or missing file leaves the bundled one in place).
  useEffect(() => {
    setHidden(readMascotHidden());
    setMounted(true);
    noteMascotActivity();
    let cancelled = false;
    fetch(MANIFEST_URL, { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null))
      .then(json => {
        if (!cancelled && isMascotManifest(json)) setMascotManifest(json);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // First-run nudge: the setup page is where a fresh install lands. Once per
  // browser session (the store dedupes by id and skips a hidden mascot).
  useEffect(() => {
    if (!mounted || hidden || !pathname?.startsWith('/setup')) return;
    sayMascotTip(
      "Hi! I'm your hive guide. If anything on this page looks off, click me and ask the Wizard.",
      { id: 'welcome-setup', ttlMs: 15_000 },
    );
  }, [mounted, hidden, pathname]);

  // Menu open: move focus into it and close on a pointer-down outside.
  // (Escape and focus-leaving are handled on the widget itself below.)
  useEffect(() => {
    if (!menuOpen) return;
    menuItems(menuRef.current)[0]?.focus();
    const onPointer = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    return () => {
      document.removeEventListener('mousedown', onPointer);
    };
  }, [menuOpen]);

  const closeMenu = useCallback((restoreFocus: boolean) => {
    setMenuOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const toggleMenu = useCallback(() => {
    // Clicking a dozing mascot wakes it (and counts as activity).
    setMascotState('idle');
    setMenuOpen(open => !open);
  }, []);

  // focusout: when focus lands on something outside the widget (Tab past the
  // last item, a click on another control) the menu is done. relatedTarget
  // is null when focus falls to <body>; the mousedown listener covers that.
  const onRootBlur = useCallback((e: React.FocusEvent<HTMLDivElement>) => {
    if (!menuOpen) return;
    const next = e.relatedTarget as Node | null;
    if (next && !e.currentTarget.contains(next)) setMenuOpen(false);
  }, [menuOpen]);

  const onWidgetKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!menuOpen) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      closeMenu(true);
      return;
    }
    const items = menuItems(menuRef.current);
    if (items.length === 0) return;
    const idx = items.indexOf(document.activeElement as HTMLElement);
    let target: HTMLElement | undefined;
    switch (e.key) {
      case 'ArrowDown':
        target = items[idx < 0 ? 0 : (idx + 1) % items.length];
        break;
      case 'ArrowUp':
        target = items[idx < 0 ? items.length - 1 : (idx - 1 + items.length) % items.length];
        break;
      case 'Home':
        target = items[0];
        break;
      case 'End':
        target = items[items.length - 1];
        break;
      default:
        return;
    }
    e.preventDefault();
    target?.focus();
  }, [menuOpen, closeMenu]);

  const hide = useCallback(() => {
    writeMascotHidden(true);
    setHidden(true);
    setMenuOpen(false);
    dismissMascotTip();
  }, []);

  const show = useCallback(() => {
    writeMascotHidden(false);
    setHidden(false);
    noteMascotActivity();
  }, []);

  const askWizard = useCallback(() => {
    setMenuOpen(false);
    router.push('/wizard');
  }, [router]);

  if (!mounted) return null;

  if (hidden) {
    return (
      <button
        type="button"
        onClick={show}
        title="Show mascot"
        aria-label="Show mascot"
        className="opacity-50 hover:opacity-100 transition-opacity"
        style={{
          ...CORNER,
          width: 14,
          height: 14,
          background: '#76B900',
          clipPath: 'polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%)',
        }}
      />
    );
  }

  const strip = resolveStrip(manifest, state);
  const spriteStyle: SpriteStyle = {
    '--mascot-sheet': `url("${manifest.sheet}")`,
    '--mascot-fw': `${manifest.frameWidth}px`,
    '--mascot-fh': `${manifest.frameHeight}px`,
    '--mascot-start-col': strip.startCol,
    '--mascot-end-col': strip.startCol + strip.count - 1,
    '--mascot-frames': strip.count,
    '--mascot-fps': strip.fps,
    display: 'block',
    width: 'var(--mascot-fw)',
    height: 'var(--mascot-fh)',
    backgroundImage: 'var(--mascot-sheet)',
    backgroundRepeat: 'no-repeat',
    backgroundPositionX: 'calc(var(--mascot-start-col) * var(--mascot-fw) * -1)',
    backgroundPositionY: `${-strip.row * manifest.frameHeight}px`,
    imageRendering: 'pixelated',
    animationName: 'mascot-strip',
    animationDuration: 'calc(var(--mascot-frames) / var(--mascot-fps) * 1s)',
    animationTimingFunction: 'steps(var(--mascot-frames), jump-none)',
    animationIterationCount: strip.loop ? 'infinite' : 1,
    animationFillMode: 'forwards',
  };

  const label = MASCOT_STATE_LABELS[state];

  return (
    <div ref={rootRef} style={CORNER} className="flex flex-col items-end" onBlur={onRootBlur}>
      {tip && (
        <div
          role="status"
          aria-live="polite"
          className="mb-2 flex items-start gap-2 rounded-md border px-3 py-2 text-xs leading-snug shadow-lg"
          style={{
            maxWidth: 240,
            background: 'var(--bg-card)',
            borderColor: 'var(--border-green)',
            color: 'var(--text-primary)',
            animation: 'mascot-bubble-in 0.18s ease-out',
          }}
        >
          <span className="min-w-0 flex-1">{tip.text}</span>
          <button
            type="button"
            onClick={dismissMascotTip}
            aria-label="Dismiss tip"
            className="-mr-1 -mt-0.5 px-1 leading-none hover:text-[#76B900]"
            style={{ color: 'var(--text-muted)' }}
          >
            ×
          </button>
        </div>
      )}
      <div className="relative" onKeyDown={onWidgetKeyDown}>
        <button
          ref={triggerRef}
          type="button"
          onClick={toggleMenu}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-controls={menuOpen ? MENU_ID : undefined}
          title={`Mascot: ${label}. Click for options.`}
          className="block rounded-md p-0 hover:drop-shadow-[0_0_6px_rgba(118,185,0,0.6)]"
          style={{ background: 'transparent', lineHeight: 0 }}
        >
          <span
            key={state}
            role="img"
            aria-label={`nvHive mascot: ${label}`}
            className="nvh-mascot-sprite"
            style={spriteStyle}
          />
        </button>
        {menuOpen && (
          <div
            ref={menuRef}
            id={MENU_ID}
            role="menu"
            aria-label="Mascot menu"
            className="absolute bottom-0 flex flex-col overflow-hidden rounded-md border text-xs shadow-lg"
            style={{
              right: 'calc(100% + 0.5rem)',
              minWidth: 140,
              background: 'var(--bg-card)',
              borderColor: 'var(--border)',
              animation: 'mascot-bubble-in 0.12s ease-out',
            }}
          >
            <button
              type="button"
              role="menuitem"
              onClick={askWizard}
              className="px-3 py-2 text-left hover:bg-[var(--bg-subtle)] focus:bg-[var(--bg-subtle)] focus:outline-none"
              style={{ color: 'var(--text-primary)' }}
            >
              Ask the Wizard
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={hide}
              className="border-t px-3 py-2 text-left hover:bg-[var(--bg-subtle)] focus:bg-[var(--bg-subtle)] focus:outline-none"
              style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}
            >
              Hide mascot
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
