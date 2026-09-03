'use client';

/**
 * CreateAgentModal — wizard-style form to author a new agent profile and
 * give it a portrait. Round-trips through:
 *   POST   /v1/wizard/profiles                          (persist)
 *   POST   /v1/wizard/profiles/{name}/avatar/upload     (user-uploaded image)
 *   POST   /v1/wizard/profiles/{name}/avatar/generate   (ComfyUI; falls
 *                                                       back to "upload your
 *                                                       own" when not wired)
 *
 * Kept intentionally simple: title + description + persona prompt + a couple
 * of LLM knobs (provider/model/temperature) + portrait. Power users can edit
 * the YAML directly under NVH_HOME/agent-profiles/.
 */

import { useEffect, useRef, useState } from 'react';
import AgentAvatar from '@/components/AgentAvatar';
import {
  listAgentProfiles,
  profileAvatarUrl,
  saveAgentProfile,
  uploadAgentAvatar,
  type AgentProfileSchema,
} from '@/lib/api';

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated?: (name: string) => void;
  /** Tool names from the live /v1/wizard/tools catalog (WizardChat already
   * fetches it). When provided and non-empty these are the options; the
   * static list below is only the fallback for hosts without the catalog. */
  toolNames?: string[];
}

/** Fallback whitelist options — keep in sync with the shipped registry
 * (nvh/integrations/wizard/tools.py + home_assistant.py). Prefer passing
 * `toolNames` from the catalog so a new tool never needs a UI edit. */
const TOOL_OPTIONS = [
  'diagnose',
  'refresh_models',
  'repair_workspace',
  'validate_provider_key',
  'save_provider_key',
  'rag_ask',
  'rag_ingest',
  'rag_ask_vault',
  'web_search',
  'home_assistant_status',
  'home_assistant_entities',
  'home_assistant_state',
  'home_assistant_services',
  'home_assistant_call',
  // Device settings (proposal §3.4). The first two are read-only / dry-run;
  // the rest are the privileged (sudo) tier — whitelisting one here still
  // leaves it gated by the red approval card and by NVH_ALLOW_PRIVILEGED.
  'system_settings_get',
  'system_settings_plan',
  'system_settings_apply',
  'apt_install',
  'snap_install',
  'service_enable',
  // Spark playbooks (proposal §3.5). `playbook_list` / `playbook_plan` are
  // read-only (catalogue + compiled plan); `playbook_install` is privileged
  // and runs only behind the red approval card.
  'playbook_list',
  'playbook_plan',
  'playbook_install',
];

export default function CreateAgentModal({ open, onClose, onCreated, toolNames }: Props) {
  const toolOptions = toolNames && toolNames.length > 0 ? toolNames : TOOL_OPTIONS;
  const [name, setName] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [temperature, setTemperature] = useState<string>('');
  const [toolsAllowed, setToolsAllowed] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Declared before the early return below so hook order is stable.
  const pendingUpload = useRef<File | null>(null);

  useEffect(() => {
    if (!open) return;
    setName('');
    setTitle('');
    setDescription('');
    setSystemPrompt('');
    setProvider('');
    setModel('');
    setTemperature('');
    setToolsAllowed(new Set());
    setError(null);
    setAvatarPreview(null);
  }, [open]);

  if (!open) return null;

  const slug = name.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
  const canSave = slug.length > 0 && title.trim().length > 0 && systemPrompt.trim().length > 0;

  const pickFile = () => fileInputRef.current?.click();

  const onFile = async (file: File) => {
    if (!slug) {
      setError('Pick a name first so we know where to save the avatar.');
      return;
    }
    setError(null);
    try {
      // Local preview is instant; the server upload happens at save time so
      // we don't litter NVH_HOME with abandoned avatars.
      const reader = new FileReader();
      reader.onload = () => setAvatarPreview(typeof reader.result === 'string' ? reader.result : null);
      reader.readAsDataURL(file);
      // Stash the file on the ref for save-time upload.
      pendingUpload.current = file;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'preview failed');
    }
  };


  const handleGenerate = async () => {
    if (!slug) {
      setError('Pick a name first so the portrait can be saved against it.');
      return;
    }
    setGenerating(true);
    setError(null);
    try {
      // We POST to the generate endpoint optimistically; ComfyUI integration
      // may return ok=false with a clear hint, which we surface as an info
      // message rather than an error (it's the expected first-run state).
      const res = await fetch(
        `${getApiBaseLocal()}/v1/wizard/profiles/${encodeURIComponent(slug)}/avatar/generate`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            // Auth: the global api.ts handles this for us via fetch wrappers,
            // but the modal's small-surface direct call mirrors that pattern.
          },
          body: JSON.stringify({
            name: slug,
            prompt: description ? `Portrait avatar of an AI agent. ${description}. plain studio background, soft lighting, head-and-shoulders framing, no text.` : null,
            style: 'portrait',
          }),
        },
      );
      const env = await res.json();
      const ok = env?.data?.ok;
      if (ok) {
        // Bust the cache so the avatar reload picks up the new image.
        setAvatarPreview(`/v1/wizard/profiles/${slug}/avatar?t=${Date.now()}`);
      } else {
        setError(env?.data?.hint || env?.data?.error || 'ComfyUI portrait generation isn’t configured. Upload an image instead.');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'generate failed');
    } finally {
      setGenerating(false);
    }
  };

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      const tools = toolsAllowed.size > 0 ? Array.from(toolsAllowed) : null;
      const temp = temperature ? Number.parseFloat(temperature) : null;
      await saveAgentProfile({
        name: slug,
        title: title.trim(),
        description: description.trim(),
        system_prompt: systemPrompt.trim(),
        provider: provider.trim(),
        model: model.trim(),
        temperature: Number.isFinite(temp) ? (temp as number) : null,
        max_tokens: null,
        tools_allowed: tools,
        tags: [],
      });
      if (pendingUpload.current) {
        await uploadAgentAvatar(slug, pendingUpload.current);
      }
      onCreated?.(slug);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'save failed');
    } finally {
      setSaving(false);
    }
  };

  const previewProfile = {
    name: slug || 'new',
    title: title || 'New Agent',
    description,
    system_prompt: systemPrompt,
    provider, model,
    temperature: null, max_tokens: null,
    tools_allowed: null,
    built_in: false, tags: [],
    avatar: avatarPreview ?? '',
  } as AgentProfileSchema;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.5)' }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="w-full max-w-2xl rounded-lg border shadow-xl"
        style={{ background: 'var(--bg-card)', borderColor: 'var(--border)' }}
      >
        <div className="flex items-center justify-between border-b px-4 py-3" style={{ borderColor: 'var(--border)' }}>
          <div className="text-sm font-mono font-bold uppercase tracking-[0.18em]" style={{ color: '#76B900' }}>
            Create Agent
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-sm font-mono"
            style={{ color: 'var(--text-muted)' }}
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-4 p-4 sm:flex-row">
          {/* Avatar pane */}
          <div className="flex w-full flex-col items-center gap-2 sm:w-40">
            <AgentAvatar profile={previewProfile} size="lg" />
            <button
              type="button"
              onClick={handleGenerate}
              disabled={generating || !slug}
              className="btn-primary w-full px-2 py-1 text-[10px] font-mono disabled:opacity-50"
            >
              {generating ? 'Generating…' : 'Generate Portrait'}
            </button>
            <button
              type="button"
              onClick={pickFile}
              disabled={!slug}
              className="btn-secondary w-full px-2 py-1 text-[10px] font-mono disabled:opacity-50"
            >
              Upload Image
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp,image/svg+xml"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void onFile(f);
              }}
            />
          </div>

          {/* Form pane */}
          <div className="flex flex-1 flex-col gap-2">
            <label className="flex flex-col gap-1 text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
              Agent name (used as id)
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. my-helper"
                className="rounded-sm border px-2 py-1 text-xs font-mono"
                style={{ background: 'var(--bg-card)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
              />
              {name && slug !== name && (
                <span style={{ color: 'var(--text-faint)' }}>Saved as: <span className="text-[#76B900]">{slug}</span></span>
              )}
            </label>

            <label className="flex flex-col gap-1 text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
              Display title
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="My Helper"
                className="rounded-sm border px-2 py-1 text-xs font-mono"
                style={{ background: 'var(--bg-card)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
              />
            </label>

            <label className="flex flex-col gap-1 text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
              One-line description
              <input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What this agent is good at."
                className="rounded-sm border px-2 py-1 text-xs font-mono"
                style={{ background: 'var(--bg-card)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
              />
            </label>

            <label className="flex flex-col gap-1 text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
              Persona / system prompt
              <textarea
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
                rows={4}
                placeholder="You are…"
                className="resize-none rounded-sm border px-2 py-1 text-xs font-mono"
                style={{ background: 'var(--bg-card)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
              />
            </label>

            <div className="grid grid-cols-3 gap-2">
              <label className="flex flex-col gap-1 text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
                Provider
                <input
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  placeholder="ollama"
                  className="rounded-sm border px-2 py-1 text-xs font-mono"
                  style={{ background: 'var(--bg-card)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
                />
              </label>
              <label className="flex flex-col gap-1 text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
                Model
                <input
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="ollama/qwen2.5:7b"
                  className="rounded-sm border px-2 py-1 text-xs font-mono"
                  style={{ background: 'var(--bg-card)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
                />
              </label>
              <label className="flex flex-col gap-1 text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
                Temperature
                <input
                  value={temperature}
                  onChange={(e) => setTemperature(e.target.value)}
                  placeholder="0.4"
                  className="rounded-sm border px-2 py-1 text-xs font-mono"
                  style={{ background: 'var(--bg-card)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
                />
              </label>
            </div>

            <div className="text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
              Allowed tools (none = all)
              <div className="mt-1 flex flex-wrap gap-1">
                {toolOptions.map(name => {
                  const active = toolsAllowed.has(name);
                  return (
                    <button
                      key={name}
                      type="button"
                      onClick={() => setToolsAllowed(prev => {
                        const next = new Set(prev);
                        if (next.has(name)) next.delete(name); else next.add(name);
                        return next;
                      })}
                      className="rounded-sm border px-1.5 py-0.5 text-[9px] font-mono"
                      style={{
                        background: active ? '#76B900' : 'var(--bg-subtle)',
                        color: active ? '#ffffff' : 'var(--text-secondary)',
                        borderColor: active ? '#76B900' : 'var(--border)',
                      }}
                    >
                      {name}
                    </button>
                  );
                })}
              </div>
            </div>

            {error && (
              <div className="rounded-sm border px-2 py-1 text-[10px] font-mono" style={{ borderColor: '#dc2626', background: 'rgba(220,38,38,0.08)', color: '#dc2626' }}>
                {error}
              </div>
            )}

            <div className="mt-2 flex gap-2">
              <button
                type="button"
                onClick={handleSave}
                disabled={!canSave || saving}
                className="btn-primary px-3 py-1.5 text-xs font-mono disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save Agent'}
              </button>
              <button
                type="button"
                onClick={onClose}
                className="btn-secondary px-3 py-1.5 text-xs font-mono"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function getApiBaseLocal(): string {
  // The modal posts directly because it composes form-data with a JSON body
  // and an optional file upload, both of which the wrapper helpers handle
  // separately. We mirror the wrapper's base-selection.
  if (typeof window === 'undefined') return '';
  const runtime = (window as unknown as { __HIVE_API_URL__?: string }).__HIVE_API_URL__;
  if (runtime) return runtime.replace(/\/+$/, '');
  return '';
}
