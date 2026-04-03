'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useRef, useState, useCallback } from 'react';
import { checkHealth } from '@/lib/api';
import type { ConversationSummary } from '@/lib/types';

// ─── Icons ────────────────────────────────────────────────────────────────────

function IconSystem() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" />
    </svg>
  );
}

function IconProviders() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0a3 3 0 01-3 3m0 3h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008zm-3 6h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008z" />
    </svg>
  );
}

function IconSettings() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  );
}

function IconIntegrations() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m9.193-9.193a4.5 4.5 0 00-6.364 6.364l4.5 4.5a4.5 4.5 0 006.364-6.364l-1.757-1.757" />
    </svg>
  );
}

function IconSetup() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}

function IconAnalytics() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
    </svg>
  );
}

function IconChat() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
    </svg>
  );
}

/* NVIDIA-themed Hive logo mark */
function HiveLogo() {
  return (
    <svg className="w-7 h-7 flex-shrink-0" viewBox="0 0 28 28" fill="none">
      <polygon points="14,1 27,7.5 27,20.5 14,27 1,20.5 1,7.5" stroke="#76B900" strokeWidth="1.2" fill="none" />
      <polygon points="14,6 22,10.5 22,17.5 14,22 6,17.5 6,10.5" fill="rgba(118,185,0,0.1)" stroke="rgba(118,185,0,0.4)" strokeWidth="0.8" />
      <polygon points="14,10 18,14 14,18 10,14" fill="#76B900" />
      <circle cx="14" cy="2.5" r="1.2" fill="#76B900" />
      <circle cx="25.5" cy="8.5" r="1.2" fill="#76B900" opacity="0.6" />
      <circle cx="25.5" cy="19.5" r="1.2" fill="#76B900" opacity="0.4" />
      <circle cx="14" cy="25.5" r="1.2" fill="#76B900" opacity="0.4" />
      <circle cx="2.5" cy="19.5" r="1.2" fill="#76B900" opacity="0.6" />
      <circle cx="2.5" cy="8.5" r="1.2" fill="#76B900" />
    </svg>
  );
}

// ─── Date grouping ────────────────────────────────────────────────────────────

function groupConversations(convs: ConversationSummary[]) {
  const now = Date.now();
  const today: ConversationSummary[] = [];
  const yesterday: ConversationSummary[] = [];
  const week: ConversationSummary[] = [];
  const older: ConversationSummary[] = [];
  const pinned: ConversationSummary[] = [];

  const dayMs = 86400000;

  for (const c of convs) {
    if (c.pinned) {
      pinned.push(c);
      continue;
    }
    const age = now - c.updated_at;
    if (age < dayMs) today.push(c);
    else if (age < 2 * dayMs) yesterday.push(c);
    else if (age < 7 * dayMs) week.push(c);
    else older.push(c);
  }

  return { pinned, today, yesterday, week, older };
}

function modeIcon(mode: ConversationSummary['mode']): string {
  if (mode === 'council') return '◈';
  if (mode === 'compare') return '▣';
  return '▶';
}

function modeColor(mode: ConversationSummary['mode']): string {
  if (mode === 'council') return '#76B900';
  if (mode === 'compare') return '#999999';
  return '#555555';
}

// ─── Context menu ─────────────────────────────────────────────────────────────

interface ContextMenuState {
  x: number;
  y: number;
  conversationId: string;
}

// ─── Conversation item ────────────────────────────────────────────────────────

interface ConvItemProps {
  conv: ConversationSummary;
  active: boolean;
  onClick: (id: string) => void;
  onContextMenu: (e: React.MouseEvent, id: string) => void;
  collapsed: boolean;
}

function ConvItem({ conv, active, onClick, onContextMenu, collapsed }: ConvItemProps) {
  if (collapsed) {
    return (
      <button
        onClick={() => onClick(conv.id)}
        onContextMenu={e => onContextMenu(e, conv.id)}
        title={conv.title}
        className={`w-full flex items-center justify-center py-2 transition-all ${
          active
            ? 'bg-[#76B900]/10 border-l-2 border-[#76B900] text-[#76B900]'
            : 'text-[#555555] hover:text-[#999999] hover:bg-[#1a1a1a]'
        }`}
      >
        <span className="text-[10px]" style={{ color: modeColor(conv.mode) }}>
          {modeIcon(conv.mode)}
        </span>
      </button>
    );
  }

  return (
    <button
      onClick={() => onClick(conv.id)}
      onContextMenu={e => onContextMenu(e, conv.id)}
      className={`w-full flex items-start gap-2 px-3 py-2 text-left transition-all group relative ${
        active
          ? 'bg-[#76B900]/8 border-l-2 border-[#76B900]'
          : 'border-l-2 border-transparent hover:bg-[#1a1a1a] hover:border-[#333333]'
      }`}
    >
      <span
        className="text-[10px] mt-0.5 flex-shrink-0"
        style={{ color: modeColor(conv.mode) }}
      >
        {modeIcon(conv.mode)}
      </span>
      <div className="flex-1 min-w-0">
        <div className={`text-xs truncate leading-snug ${active ? 'text-white' : 'text-[#888888] group-hover:text-[#bbbbbb]'}`}>
          {conv.title}
        </div>
        <div className="text-[9px] font-mono text-[#444444] mt-0.5 flex items-center gap-1.5">
          {conv.provider && <span>{conv.provider}</span>}
          <span>{conv.message_count} msgs</span>
        </div>
      </div>
      {conv.pinned && (
        <span className="text-[8px] text-[#76B900] flex-shrink-0 mt-0.5">📌</span>
      )}
    </button>
  );
}

// ─── Main Sidebar ─────────────────────────────────────────────────────────────

interface SidebarProps {
  conversations?: ConversationSummary[];
  activeConversationId?: string | null;
  onNewChat?: () => void;
  onSelectConversation?: (id: string) => void;
  onRenameConversation?: (id: string, newTitle: string) => void;
  onDeleteConversation?: (id: string) => void;
  onPinConversation?: (id: string) => void;
}

const BOTTOM_NAV = [
  { href: '/system', label: 'System', icon: <IconSystem /> },
  { href: '/providers', label: 'Advisors', icon: <IconProviders /> },
  { href: '/analytics', label: 'Analytics', icon: <IconAnalytics /> },
  { href: '/integrations', label: 'Integrations', icon: <IconIntegrations /> },
  { href: '/settings', label: 'Settings', icon: <IconSettings /> },
  { href: '/setup', label: 'Setup', icon: <IconSetup /> },
];

export default function Sidebar({
  conversations = [],
  activeConversationId = null,
  onNewChat,
  onSelectConversation,
  onRenameConversation,
  onDeleteConversation,
  onPinConversation,
}: SidebarProps) {
  const pathname = usePathname();
  const [connected, setConnected] = useState<boolean | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [search, setSearch] = useState('');
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const contextMenuRef = useRef<HTMLDivElement>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);

  // Close context menu on outside click
  useEffect(() => {
    if (!contextMenu) return;
    const handler = (e: MouseEvent) => {
      if (contextMenuRef.current && !contextMenuRef.current.contains(e.target as Node)) {
        setContextMenu(null);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [contextMenu]);

  // Ctrl+B to toggle sidebar
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
        e.preventDefault();
        setCollapsed(prev => !prev);
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, []);

  // Focus rename input when it appears
  useEffect(() => {
    if (renamingId && renameInputRef.current) {
      renameInputRef.current.focus();
      renameInputRef.current.select();
    }
  }, [renamingId]);

  // API health check
  useEffect(() => {
    let mounted = true;
    const check = async () => {
      try {
        await checkHealth();
        if (mounted) setConnected(true);
      } catch {
        if (mounted) setConnected(false);
      }
    };
    check();
    const interval = setInterval(check, 30_000);
    return () => { mounted = false; clearInterval(interval); };
  }, []);

  const handleContextMenu = useCallback((e: React.MouseEvent, id: string) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, conversationId: id });
  }, []);

  const handleRenameSubmit = useCallback(() => {
    if (renamingId && renameValue.trim()) {
      onRenameConversation?.(renamingId, renameValue.trim());
    }
    setRenamingId(null);
    setRenameValue('');
  }, [renamingId, renameValue, onRenameConversation]);

  const filteredConvs = search.trim()
    ? conversations.filter(c =>
        c.title.toLowerCase().includes(search.toLowerCase())
      )
    : conversations;

  const groups = groupConversations(filteredConvs);

  const renderGroup = (label: string, items: ConversationSummary[]) => {
    if (items.length === 0) return null;
    return (
      <div key={label}>
        {!collapsed && (
          <div className="px-3 py-1.5 text-[9px] font-mono text-[#3a3a3a] uppercase tracking-[0.15em]">
            {label}
          </div>
        )}
        {items.map(conv => (
          renamingId === conv.id ? (
            <div key={conv.id} className="px-3 py-1.5">
              <input
                ref={renameInputRef}
                value={renameValue}
                onChange={e => setRenameValue(e.target.value)}
                onBlur={handleRenameSubmit}
                onKeyDown={e => {
                  if (e.key === 'Enter') handleRenameSubmit();
                  if (e.key === 'Escape') { setRenamingId(null); setRenameValue(''); }
                }}
                className="w-full bg-[#1a1a1a] border border-[#76B900]/60 text-white text-xs font-mono px-2 py-1 focus:outline-none"
              />
            </div>
          ) : (
            <ConvItem
              key={conv.id}
              conv={conv}
              active={activeConversationId === conv.id}
              onClick={id => onSelectConversation?.(id)}
              onContextMenu={handleContextMenu}
              collapsed={collapsed}
            />
          )
        ))}
      </div>
    );
  };

  const isAtRoot = pathname === '/';

  return (
    <>
      <aside
        className={`flex flex-col bg-[#0d0d0d] border-r border-[#222222] h-[calc(100vh-2rem)] sticky top-8 transition-all duration-300 ${
          collapsed ? 'w-14' : 'w-64'
        }`}
      >
        {/* Logo header */}
        <div className={`flex items-center gap-3 px-3 py-3 border-b border-[#222222] ${collapsed ? 'justify-center' : ''}`}>
          <HiveLogo />
          {!collapsed && (
            <div className="flex-1 min-w-0">
              <div className="font-bold text-white text-sm leading-none tracking-wide">HIVE</div>
              <div className="text-[9px] text-[#76B900] font-mono uppercase tracking-[0.2em] mt-0.5">AI Command Center</div>
            </div>
          )}
        </div>

        {/* New Chat button */}
        <div className="px-2 pt-2 pb-1">
          <button
            onClick={onNewChat}
            className={`w-full flex items-center gap-2 px-3 py-2 text-xs font-mono font-semibold
              bg-[#76B900]/10 hover:bg-[#76B900]/20 border border-[#76B900]/30 hover:border-[#76B900]/60
              text-[#76B900] transition-all ${collapsed ? 'justify-center' : ''}`}
            title="New Chat"
          >
            <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
            {!collapsed && <span className="uppercase tracking-wider">New Chat</span>}
          </button>
        </div>

        {/* Search box */}
        {!collapsed && (
          <div className="px-2 pb-2">
            <div className="relative">
              <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3 h-3 text-[#444444]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
              </svg>
              <input
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Search chats..."
                className="w-full bg-[#111111] border border-[#2a2a2a] text-[#888888] text-xs font-mono pl-7 pr-3 py-1.5
                  focus:outline-none focus:border-[#76B900]/40 placeholder-[#333333] transition-colors"
              />
            </div>
          </div>
        )}

        {/* Conversation list */}
        <div className="flex-1 overflow-y-auto">
          {/* Chat icon when collapsed — clicking goes home */}
          {collapsed && (
            <button
              onClick={() => onSelectConversation?.('')}
              className={`w-full flex items-center justify-center py-2.5 ${
                isAtRoot ? 'text-[#76B900]' : 'text-[#555555] hover:text-[#999999]'
              }`}
              title="Chat"
            >
              <IconChat />
            </button>
          )}

          {conversations.length === 0 && !collapsed ? (
            <div className="px-3 py-6 text-center">
              <div className="text-[10px] font-mono text-[#333333] uppercase tracking-wider">
                No conversations yet
              </div>
              <div className="text-[9px] font-mono text-[#2a2a2a] mt-1">
                Start a new chat above
              </div>
            </div>
          ) : (
            <div className="pb-2">
              {renderGroup('Pinned', groups.pinned)}
              {renderGroup('Today', groups.today)}
              {renderGroup('Yesterday', groups.yesterday)}
              {renderGroup('Previous 7 Days', groups.week)}
              {renderGroup('Older', groups.older)}
            </div>
          )}
        </div>

        {/* Bottom nav links */}
        <div className="border-t border-[#222222]">
          {BOTTOM_NAV.map(item => {
            const isActive = pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                title={collapsed ? item.label : undefined}
                className={`flex items-center gap-3 px-3 py-2 text-xs font-medium transition-all ${
                  isActive
                    ? 'nav-active text-white'
                    : 'text-[#555555] hover:text-[#888888] hover:bg-[#1a1a1a]'
                } ${collapsed ? 'justify-center' : ''}`}
              >
                <span className={`flex-shrink-0 ${isActive ? 'text-[#76B900]' : 'text-[#444444]'}`}>
                  {item.icon}
                </span>
                {!collapsed && <span className="tracking-wide">{item.label}</span>}
              </Link>
            );
          })}

          {/* API status + collapse toggle */}
          <div className="border-t border-[#1a1a1a]">
            {!collapsed && (
              <div className="px-3 py-2 flex items-center gap-2">
                <span
                  className={`w-1.5 h-1.5 flex-shrink-0 ${
                    connected === null ? 'bg-[#444444] animate-pulse' :
                    connected ? 'bg-[#76B900]' : 'bg-[#ef4444]'
                  }`}
                  style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }}
                />
                <span className="text-[9px] font-mono text-[#444444]">
                  {connected === null ? 'CHECKING...' : connected ? 'API ONLINE' : 'API OFFLINE'}
                </span>
                <span className="ml-auto text-[9px] font-mono text-[#2a2a2a]">Ctrl+B</span>
              </div>
            )}
            <button
              onClick={() => setCollapsed(!collapsed)}
              className={`w-full flex items-center gap-2 px-3 py-2 text-[#444444] hover:text-[#76B900] hover:bg-[#1a1a1a] transition-all text-xs ${
                collapsed ? 'justify-center' : ''
              }`}
              title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            >
              <svg
                className={`w-3.5 h-3.5 transition-transform duration-300 ${collapsed ? 'rotate-180' : ''}`}
                fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
              </svg>
              {!collapsed && <span className="font-mono text-[9px] tracking-widest">COLLAPSE</span>}
            </button>
          </div>
        </div>
      </aside>

      {/* Context menu */}
      {contextMenu && (
        <div
          ref={contextMenuRef}
          className="fixed z-50 bg-[#1a1a1a] border border-[#333333] shadow-xl py-1 min-w-[160px]"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          {[
            {
              label: 'Rename',
              icon: '✏',
              action: () => {
                const conv = conversations.find(c => c.id === contextMenu.conversationId);
                setRenameValue(conv?.title ?? '');
                setRenamingId(contextMenu.conversationId);
                setContextMenu(null);
              },
            },
            {
              label: conversations.find(c => c.id === contextMenu.conversationId)?.pinned ? 'Unpin' : 'Pin',
              icon: '📌',
              action: () => {
                onPinConversation?.(contextMenu.conversationId);
                setContextMenu(null);
              },
            },
            {
              label: 'Export',
              icon: '↗',
              action: () => {
                // Export as JSON — placeholder
                setContextMenu(null);
              },
            },
            { separator: true },
            {
              label: 'Delete',
              icon: '✕',
              danger: true,
              action: () => {
                onDeleteConversation?.(contextMenu.conversationId);
                setContextMenu(null);
              },
            },
          ].map((item, i) => {
            if ('separator' in item && item.separator) {
              return <div key={i} className="border-t border-[#2a2a2a] my-1" />;
            }
            const menuItem = item as { label: string; icon: string; action: () => void; danger?: boolean };
            return (
              <button
                key={i}
                onClick={menuItem.action}
                className={`w-full flex items-center gap-2.5 px-3 py-1.5 text-xs font-mono transition-colors text-left ${
                  menuItem.danger
                    ? 'text-[#ef4444] hover:bg-[#ef4444]/10'
                    : 'text-[#999999] hover:text-white hover:bg-[#2a2a2a]'
                }`}
              >
                <span className="text-[10px]">{menuItem.icon}</span>
                {menuItem.label}
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}
