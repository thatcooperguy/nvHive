'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import PageHeader from '@/components/PageHeader';
import { getVaultStatus, initVault, installObsidian, saveVaultMemory } from '@/lib/api';
import type { VaultStatus } from '@/lib/types';

const CATEGORIES = [
  'Wizard Memory',
  'Decisions',
  'Conflicts',
  'Troubleshooting',
  'Process Playbooks',
  'Projects',
  'Models',
  'ComfyUI',
  'System',
];

function statusLabel(status: VaultStatus | null): string {
  if (!status) return 'checking';
  if (!status.initialized) return 'needs setup';
  if (status.obsidian.installed) return 'vault + obsidian';
  return 'vault ready';
}

function StatCard({ label, value, tone = 'green' }: { label: string; value: string | number; tone?: 'green' | 'amber' | 'gray' }) {
  const toneClass = tone === 'amber' ? 'text-[#d97706]' : tone === 'gray' ? 'text-[#737373]' : 'text-[#76B900]';
  return (
    <div className="border border-[#e5e5e5] bg-white p-4 dark:bg-[#0a0a0a] dark:border-[#262626]">
      <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-[#737373] dark:text-[#a3a3a3]">{label}</div>
      <div className={`mt-2 font-mono text-lg font-bold ${toneClass}`}>{value}</div>
    </div>
  );
}

function InlineMessage({ message, error }: { message: string | null; error?: boolean }) {
  if (!message) return null;
  return (
    <div className={`border px-4 py-3 text-sm ${error ? 'border-red-200 bg-red-50 text-red-700' : 'border-[#76B900]/30 bg-[#76B900]/5 text-[#447000]'}`}>
      {message}
    </div>
  );
}

export default function VaultPage() {
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState('');
  const [category, setCategory] = useState('Wizard Memory');
  const [body, setBody] = useState('');
  const [tags, setTags] = useState('pilot,rootless');

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await getVaultStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load vault status');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const seedCount = useMemo(() => {
    if (!status?.seeded_files) return 0;
    return Object.keys(status.seeded_files).length;
  }, [status]);

  const handleInit = async () => {
    setWorking('vault');
    setMessage(null);
    setError(null);
    try {
      const next = await initVault();
      setStatus(next);
      const count = Object.keys(next.seeded_files ?? {}).length;
      setMessage(count ? `Vault initialized with ${count} starter notes.` : 'Vault is already initialized.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Vault initialization failed');
    } finally {
      setWorking(null);
    }
  };

  const handleInstallObsidian = async () => {
    setWorking('obsidian');
    setMessage(null);
    setError(null);
    try {
      const obsidian = await installObsidian();
      const next = await getVaultStatus();
      setStatus({ ...next, obsidian });
      setMessage(obsidian.message || (obsidian.installed ? 'Obsidian is ready for this vault.' : obsidian.reason));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Obsidian install failed');
    } finally {
      setWorking(null);
    }
  };

  const handleSaveMemory = async () => {
    if (!title.trim() || !body.trim()) {
      setError('Add a title and memory note first.');
      return;
    }
    setWorking('memory');
    setMessage(null);
    setError(null);
    try {
      const saved = await saveVaultMemory({
        title,
        body,
        category,
        tags: tags.split(',').map(tag => tag.trim()).filter(Boolean),
      });
      setMessage(`Saved memory to ${saved.category}.`);
      setTitle('');
      setBody('');
      setStatus(await getVaultStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save memory');
    } finally {
      setWorking(null);
    }
  };

  return (
    <main>
      <PageHeader
        eyebrow="Memory"
        title="nvHive Vault"
        subtitle="Plain Markdown memory for this rootless workspace. Obsidian can open it, but nvHive owns the durable vault."
        trailing={
          <div className="flex gap-2">
            <button onClick={refresh} disabled={loading || Boolean(working)} className="btn-secondary px-4 py-2 text-xs font-mono uppercase tracking-wider">
              Refresh
            </button>
            <button onClick={handleInit} disabled={working === 'vault'} className="btn-primary px-4 py-2 text-xs font-mono uppercase tracking-wider">
              {working === 'vault' ? 'Initializing' : 'Initialize Vault'}
            </button>
            <button onClick={handleInstallObsidian} disabled={working === 'obsidian'} className="btn-secondary px-4 py-2 text-xs font-mono uppercase tracking-wider">
              {working === 'obsidian' ? 'Installing' : 'Install Obsidian'}
            </button>
          </div>
        }
      />
      <div className="p-6 space-y-6 max-w-6xl mx-auto">

      <InlineMessage message={message} />
      <InlineMessage message={error} error />

      <section className="grid gap-4 md:grid-cols-4">
        <StatCard label="Vault" value={statusLabel(status)} tone={status?.initialized ? 'green' : 'amber'} />
        <StatCard label="Notes" value={status?.markdown_files ?? (loading ? '...' : 0)} />
        <StatCard label="Seeded" value={seedCount} tone="gray" />
        <StatCard label="Obsidian" value={status?.obsidian.installed ? 'ready' : 'optional'} tone={status?.obsidian.installed ? 'green' : 'gray'} />
      </section>

      <section className="grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
        <div className="card-flat p-5 space-y-4">
          <div>
            <div className="section-label">Add Memory</div>
            <h2 className="text-xl font-bold mt-1">Capture a decision, fix, or test result</h2>
            <p className="text-sm text-[#737373] mt-1 dark:text-[#a3a3a3]">
              Use this for durable product memory that the wizard and support flow can read later.
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-[1fr_220px]">
            <input
              value={title}
              onChange={event => setTitle(event.target.value)}
              className="input-base px-3 py-2 text-sm"
              placeholder="Memory title"
            />
            <select
              value={category}
              onChange={event => setCategory(event.target.value)}
              className="input-base px-3 py-2 text-sm"
            >
              {CATEGORIES.map(item => <option key={item}>{item}</option>)}
            </select>
          </div>
          <textarea
            value={body}
            onChange={event => setBody(event.target.value)}
            className="input-base min-h-[180px] w-full px-3 py-2 text-sm"
            placeholder="What should AI Wizard remember?"
          />
          <div className="grid gap-3 md:grid-cols-[1fr_180px]">
            <input
              value={tags}
              onChange={event => setTags(event.target.value)}
              className="input-base px-3 py-2 text-sm"
              placeholder="comma,separated,tags"
            />
            <button onClick={handleSaveMemory} disabled={working === 'memory'} className="btn-primary px-4 py-2 text-xs font-mono uppercase tracking-wider">
              {working === 'memory' ? 'Saving' : 'Save Memory'}
            </button>
          </div>
        </div>

        <div className="card-flat p-5 space-y-4">
          <div>
            <div className="section-label">Rootless Paths</div>
            <h2 className="text-xl font-bold mt-1">Open the same memory anywhere</h2>
          </div>
          <div className="space-y-3 text-sm">
            <div>
              <div className="text-[10px] font-mono uppercase tracking-wider text-[#737373] dark:text-[#a3a3a3]">Vault folder</div>
              <code className="block mt-1 break-all border border-[#e5e5e5] bg-[#fafafa] px-3 py-2 text-xs dark:bg-[#141414] dark:border-[#262626]">{status?.vault_dir ?? 'loading'}</code>
            </div>
            <div>
              <div className="text-[10px] font-mono uppercase tracking-wider text-[#737373] dark:text-[#a3a3a3]">Obsidian command</div>
              <code className="block mt-1 break-all border border-[#e5e5e5] bg-[#fafafa] px-3 py-2 text-xs dark:bg-[#141414] dark:border-[#262626]">{status?.obsidian.open_command ?? 'install optional viewer'}</code>
            </div>
          </div>
          <div className="border border-[#76B900]/30 bg-[#76B900]/5 p-3 text-xs text-[#447000]">
            Vault notes are not a secrets manager. API keys stay in rootless config, not in Markdown memory.
          </div>
        </div>
      </section>

      <section className="card-flat p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="section-label">Conflicts</div>
            <h2 className="text-xl font-bold mt-1">Tradeoffs AI Wizard should explain</h2>
          </div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {(status?.conflicts ?? []).map(conflict => (
            <div key={conflict.title} className="border border-[#e5e5e5] bg-white p-4 dark:bg-[#0a0a0a] dark:border-[#262626]">
              <h3 className="font-bold text-[#0a0a0a] dark:text-[#fafafa]">{conflict.title}</h3>
              <p className="mt-1 text-sm text-[#525252] dark:text-[#a3a3a3]">{conflict.summary}</p>
            </div>
          ))}
        </div>
      </section>
      </div>
    </main>
  );
}
