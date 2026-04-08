'use client';

import { useEffect, useMemo, useRef, useCallback, useState } from 'react';
import type { ChatMode, ProviderHealth } from '@/lib/types';

interface ModelOption {
  model_id: string;
  provider: string;
  display_name: string;
  is_local?: boolean;
  cost_tier?: 'free' | 'low' | 'high';
}

interface AttachedFile {
  name: string;
  content: string;       // text content or data URL for images
  isImage: boolean;
  lang?: string;         // detected language for code files
  size: number;
}

interface ChatInputProps {
  value: string;
  onChange: (val: string) => void;
  onSubmit: () => void;
  onStop?: () => void;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  selectedModel: string;
  onModelChange: (model: string) => void;
  models: ModelOption[];
  /** Optional provider health from /v1/advisors. When supplied, models
   *  whose provider is reachable are grouped at the top of the dropdown
   *  and broken providers are labelled "offline". */
  providerHealth?: ProviderHealth[];
  streaming: boolean;
  disabled?: boolean;
}

const MODES: { value: ChatMode; label: string; icon: string }[] = [
  { value: 'single', label: 'Single', icon: '▶' },
  { value: 'council', label: 'Convene', icon: '◈' },
  { value: 'compare', label: 'Poll', icon: '▣' },
];

const ACCEPTED_EXTENSIONS = [
  '.py', '.js', '.ts', '.tsx', '.jsx',
  '.txt', '.md', '.json', '.yaml', '.yml', '.csv',
  '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp',
  '.rs', '.go', '.java', '.c', '.cpp', '.h', '.cs', '.rb', '.sh',
];

const IMAGE_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp']);

const LANG_MAP: Record<string, string> = {
  '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
  '.tsx': 'tsx', '.jsx': 'jsx', '.json': 'json',
  '.yaml': 'yaml', '.yml': 'yaml', '.md': 'markdown',
  '.sh': 'bash', '.rs': 'rust', '.go': 'go',
  '.java': 'java', '.c': 'c', '.cpp': 'cpp',
  '.h': 'c', '.cs': 'csharp', '.rb': 'ruby',
  '.csv': 'csv', '.txt': 'text',
};

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB

function getExtension(filename: string): string {
  const idx = filename.lastIndexOf('.');
  return idx >= 0 ? filename.slice(idx).toLowerCase() : '';
}

function costTierLabel(tier?: string): string {
  if (!tier || tier === 'free') return 'FREE';
  if (tier === 'low') return '$';
  return '$$';
}

function costTierColor(tier?: string): string {
  if (!tier || tier === 'free') return '#76B900';
  if (tier === 'low') return '#f59e0b';
  return '#ef4444';
}

export default function ChatInput({
  value,
  onChange,
  onSubmit,
  onStop,
  mode,
  onModeChange,
  selectedModel,
  onModelChange,
  models,
  providerHealth = [],
  streaming,
  disabled = false,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    const maxH = window.innerHeight * 0.5;
    el.style.height = `${Math.min(el.scrollHeight, maxH)}px`;
  }, [value]);

  const processFile = useCallback((file: File): Promise<AttachedFile | null> => {
    return new Promise(resolve => {
      if (file.size > MAX_FILE_SIZE) {
        setFileError(`${file.name} exceeds 10 MB limit`);
        resolve(null);
        return;
      }
      const ext = getExtension(file.name);
      const isImage = IMAGE_EXTENSIONS.has(ext);

      const reader = new FileReader();
      reader.onload = (e) => {
        const result = e.target?.result;
        if (!result) { resolve(null); return; }
        resolve({
          name: file.name,
          content: result as string,
          isImage,
          lang: isImage ? undefined : (LANG_MAP[ext] ?? 'text'),
          size: file.size,
        });
      };
      reader.onerror = () => resolve(null);

      if (isImage) {
        reader.readAsDataURL(file);
      } else {
        reader.readAsText(file);
      }
    });
  }, []);

  const handleFileSelect = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setFileError(null);
    const results = await Promise.all(Array.from(files).map(processFile));
    const valid = results.filter(Boolean) as AttachedFile[];
    setAttachedFiles(prev => [...prev, ...valid]);
  }, [processFile]);

  const handleAttachClick = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const removeFile = useCallback((name: string) => {
    setAttachedFiles(prev => prev.filter(f => f.name !== name));
  }, []);

  // Build the final prompt including file contents
  const buildPrompt = useCallback((): string => {
    if (attachedFiles.length === 0) return value;
    const fileSections = attachedFiles.map(f => {
      if (f.isImage) {
        return `User uploaded image: ${f.name}\n[Image attached — see data URL]`;
      }
      const fence = `\`\`\`${f.lang ?? ''}`;
      return `User uploaded file: ${f.name}\n${fence}\n${f.content}\n\`\`\``;
    });
    return `${fileSections.join('\n\n')}\n\n${value}`;
  }, [attachedFiles, value]);

  // Override onSubmit to inject file content first
  const handleSubmit = useCallback(() => {
    if (attachedFiles.length > 0) {
      const combined = buildPrompt();
      onChange(combined);
      // Small tick so state propagates, then submit via the passed handler
      setAttachedFiles([]);
      setTimeout(() => {
        onSubmit();
      }, 0);
    } else {
      onSubmit();
    }
  }, [attachedFiles, buildPrompt, onChange, onSubmit]);

  // Intercept onChange so we can call the real submit with injected content
  // We wrap the external onSubmit with our file-injecting version
  // For the textarea, we still use the plain onChange
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        if (!streaming && value.trim()) handleSubmit();
      }
    },
    [handleSubmit, streaming, value]
  );

  // Drag and drop
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback(() => {
    setDragOver(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    handleFileSelect(e.dataTransfer.files);
  }, [handleFileSelect]);

  const selectedModelInfo = models.find(m => m.model_id === selectedModel);

  // Split models into "connected" and "offline" buckets based on the
  // health of their backing provider. Local models (ollama/local) are
  // always treated as connected — they don't route through the same
  // advisor health system. Within each bucket, models are sorted by
  // provider latency (fastest first) then display name for stability.
  const { connectedModels, offlineModels } = useMemo(() => {
    if (providerHealth.length === 0) {
      // No health info — fall back to original order, everything "connected"
      return { connectedModels: models, offlineModels: [] as ModelOption[] };
    }
    const healthByName = new Map(providerHealth.map(p => [p.name, p]));
    const latency = (providerName: string) =>
      healthByName.get(providerName)?.latency_ms ?? Number.POSITIVE_INFINITY;
    const isHealthy = (m: ModelOption): boolean => {
      if (m.is_local) return true;
      const h = healthByName.get(m.provider);
      return h ? h.healthy : true;  // unknown provider → give benefit of doubt
    };
    const sortFn = (a: ModelOption, b: ModelOption) => {
      const la = latency(a.provider);
      const lb = latency(b.provider);
      if (la !== lb) return la - lb;
      return a.display_name.localeCompare(b.display_name);
    };
    const connected: ModelOption[] = [];
    const offline: ModelOption[] = [];
    for (const m of models) (isHealthy(m) ? connected : offline).push(m);
    connected.sort(sortFn);
    offline.sort(sortFn);
    return { connectedModels: connected, offlineModels: offline };
  }, [models, providerHealth]);

  return (
    <div
      className={`border-t bg-[#0d0d0d] p-3 transition-colors ${
        dragOver ? 'border-[#76B900] bg-[#76B900]/5' : 'border-[#333333]'
      }`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={ACCEPTED_EXTENSIONS.join(',')}
        className="hidden"
        onChange={e => handleFileSelect(e.target.files)}
      />

      {/* Mode hint */}
      {mode === 'council' && (
        <div className="flex items-center gap-2 mb-2 px-1">
          <span
            className="w-1.5 h-1.5 bg-[#76B900]"
            style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }}
          />
          <span className="text-[10px] font-mono text-[#76B900]/70">
            Multiple AI advisors will deliberate and synthesize a response
          </span>
        </div>
      )}
      {mode === 'compare' && (
        <div className="flex items-center gap-2 mb-2 px-1">
          <span className="text-[10px] font-mono text-[#999999]/70">
            ▣ All available advisors will respond for side-by-side comparison
          </span>
        </div>
      )}

      {/* Drag-over hint */}
      {dragOver && (
        <div className="flex items-center gap-2 mb-2 px-1">
          <span className="text-[10px] font-mono text-[#76B900]">
            Drop files to attach
          </span>
        </div>
      )}

      {/* File error */}
      {fileError && (
        <div className="flex items-center gap-2 mb-2 px-1">
          <span className="text-[10px] font-mono text-[#ef4444]">{fileError}</span>
          <button
            type="button"
            onClick={() => setFileError(null)}
            className="text-[#ef4444]/60 hover:text-[#ef4444] text-[10px] font-mono"
          >
            ✕
          </button>
        </div>
      )}

      {/* Attached file chips */}
      {attachedFiles.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2 px-1">
          {attachedFiles.map(f => (
            <div
              key={f.name}
              className="flex items-center gap-1.5 bg-[#111111] border border-[#76B900]/30 px-2 py-1 text-[10px] font-mono group"
            >
              {f.isImage ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={f.content}
                  alt={f.name}
                  className="w-5 h-5 object-cover flex-shrink-0 border border-[#333333]"
                />
              ) : (
                <span className="text-[#76B900] opacity-70">
                  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round"
                      d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                </span>
              )}
              <span className="text-[#cccccc] max-w-[120px] truncate">{f.name}</span>
              <button
                type="button"
                onClick={() => removeFile(f.name)}
                className="text-[#444444] hover:text-[#ef4444] transition-colors ml-0.5 flex-shrink-0"
                title="Remove file"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Main input area */}
      <div className="flex gap-2 items-end">
        {/* Attach button */}
        <button
          type="button"
          onClick={handleAttachClick}
          disabled={disabled || streaming}
          className="flex-shrink-0 w-9 h-9 flex items-center justify-center border border-[#333333] bg-[#111111] text-[#555555] hover:text-[#76B900] hover:border-[#76B900]/40 transition-colors disabled:opacity-40"
          title="Attach file (.py, .js, .ts, .txt, .md, .json, .yaml, .csv, .pdf, images)"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M18.375 12.739l-7.693 7.693a4.5 4.5 0 01-6.364-6.364l10.94-10.94A3 3 0 1119.5 7.372L8.552 18.32m.009-.01l-.01.01m5.699-9.941l-7.81 7.81a1.5 1.5 0 002.112 2.13" />
          </svg>
        </button>

        {/* Textarea */}
        <textarea
          ref={textareaRef}
          value={value}
          onChange={e => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            mode === 'council'
              ? 'Convene the hive...'
              : mode === 'compare'
              ? 'Poll across advisors...'
              : dragOver
              ? 'Drop files here...'
              : 'Send a message...'
          }
          disabled={disabled || streaming}
          rows={1}
          className={`flex-1 bg-[#111111] border text-white placeholder-[#444444] px-3 py-2.5 text-sm font-mono resize-none
            focus:outline-none focus:border-[#76B900]/60 focus:ring-1 focus:ring-[#76B900]/20
            disabled:opacity-50 transition-colors min-h-[40px] leading-relaxed ${
            dragOver ? 'border-[#76B900]/60' : 'border-[#333333]'
          }`}
          style={{ maxHeight: '50vh' }}
        />

        {/* Send / Stop button */}
        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            className="flex-shrink-0 w-9 h-9 flex items-center justify-center bg-[#ef4444]/10 border border-[#ef4444]/30 text-[#ef4444] hover:bg-[#ef4444]/20 transition-colors"
            title="Stop generation"
          >
            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
              <rect x="6" y="6" width="12" height="12" rx="1" />
            </svg>
          </button>
        ) : (
          <button
            type="button"
            onClick={handleSubmit}
            disabled={disabled || (!value.trim() && attachedFiles.length === 0)}
            className="flex-shrink-0 w-9 h-9 flex items-center justify-center bg-[#76B900] hover:bg-[#8fd000] text-black transition-all disabled:opacity-40 disabled:cursor-not-allowed"
            title="Send (Ctrl+Enter)"
            style={{ boxShadow: (value.trim() || attachedFiles.length > 0) ? '0 0 10px rgba(118,185,0,0.3)' : undefined }}
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
            </svg>
          </button>
        )}
      </div>

      {/* Bottom toolbar */}
      <div className="flex items-center gap-2 mt-2 px-1">
        {/* Mode pills */}
        <div className="flex items-center bg-[#111111] border border-[#333333] p-0.5 gap-0.5 mode-pills-container">
          {MODES.map(m => (
            <button
              key={m.value}
              type="button"
              onClick={() => onModeChange(m.value)}
              disabled={disabled || streaming}
              className={`px-3 py-1 text-[10px] font-mono uppercase tracking-wider transition-all flex items-center gap-1.5 disabled:opacity-50 ${
                mode === m.value
                  ? 'bg-[#76B900] text-black font-bold'
                  : 'text-[#555555] hover:text-[#999999] hover:bg-[#1a1a1a]'
              }`}
            >
              <span className="text-[8px]">{m.icon}</span>
              <span className="mode-label">{m.label}</span>
            </button>
          ))}
        </div>

        {/* Mode selector dropdown (mobile) */}
        <select
          value={mode}
          onChange={e => onModeChange(e.target.value as ChatMode)}
          disabled={disabled || streaming}
          className="mode-dropdown hidden bg-[#111111] border border-[#333333] text-[#999999] text-[10px] font-mono px-2 py-1
            focus:outline-none focus:border-[#76B900]/60 appearance-none disabled:opacity-50"
        >
          {MODES.map(m => (
            <option key={m.value} value={m.value}>{m.icon} {m.label}</option>
          ))}
        </select>

        {/* Model selector — only relevant in single mode */}
        {mode === 'single' && models.length > 0 && (
          <div className="relative flex items-center gap-1">
            <select
              value={selectedModel}
              onChange={e => onModelChange(e.target.value)}
              disabled={disabled || streaming}
              className="bg-[#111111] border border-[#333333] text-[#999999] text-[10px] font-mono pl-2 pr-6 py-1
                focus:outline-none focus:border-[#76B900]/60 appearance-none disabled:opacity-50 hover:border-[#444444] transition-colors"
            >
              {connectedModels.length > 0 && (
                <optgroup label={offlineModels.length > 0 ? "● Connected" : "Models"}>
                  {connectedModels.map(m => (
                    <option key={m.model_id} value={m.model_id}>
                      {m.display_name} {m.is_local ? '(local)' : '(cloud)'}
                    </option>
                  ))}
                </optgroup>
              )}
              {offlineModels.length > 0 && (
                <optgroup label="○ Offline">
                  {offlineModels.map(m => (
                    <option key={m.model_id} value={m.model_id}>
                      {m.display_name} · offline
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
            {selectedModelInfo && (
              <span
                className="text-[9px] font-mono font-bold ml-0.5"
                style={{ color: costTierColor(selectedModelInfo.cost_tier) }}
              >
                {costTierLabel(selectedModelInfo.cost_tier)}
              </span>
            )}
          </div>
        )}

        <span className="ml-auto text-[9px] font-mono text-[#2a2a2a] ctrl-enter-hint">
          Ctrl+Enter to send
        </span>
      </div>
    </div>
  );
}
