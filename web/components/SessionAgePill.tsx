// 0.46 (design §7): the session-age pill grew into the session cost pill,
// components/SessionCostPill.tsx (same `nvh_session_started_at` anchor, plus
// the desktop-rate term and cloud spend since the tab opened). LayoutShell
// imports this path's default export, so this file stays a re-export.
export { default } from './SessionCostPill';
