import type { AgentPersona } from '@/lib/types';

interface Props {
  agent: AgentPersona;
  index?: number;
  compact?: boolean;
}

const BADGE_COLORS = [
  { bg: 'bg-[#3b82f6]/10', text: 'text-[#3b82f6]', border: 'border-[#3b82f6]/20' },
  { bg: 'bg-[#a855f7]/10', text: 'text-[#a855f7]', border: 'border-[#a855f7]/20' },
  { bg: 'bg-[#22c55e]/10', text: 'text-[#22c55e]', border: 'border-[#22c55e]/20' },
  { bg: 'bg-[#f59e0b]/10', text: 'text-[#f59e0b]', border: 'border-[#f59e0b]/20' },
  { bg: 'bg-[#06b6d4]/10', text: 'text-[#06b6d4]', border: 'border-[#06b6d4]/20' },
  { bg: 'bg-[#f97316]/10', text: 'text-[#f97316]', border: 'border-[#f97316]/20' },
];

export default function AgentBadge({ agent, index = 0, compact = false }: Props) {
  const color = BADGE_COLORS[index % BADGE_COLORS.length];

  if (compact) {
    return (
      <span
        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border
          ${color.bg} ${color.text} ${color.border}`}
      >
        <span className="w-1.5 h-1.5 rounded-full bg-current opacity-70" />
        {agent.role}
      </span>
    );
  }

  return (
    <div className={`rounded-xl border p-3 ${color.bg} ${color.border}`}>
      <div className="flex items-start justify-between mb-2">
        <div className={`font-semibold text-sm ${color.text}`}>{agent.role}</div>
        {agent.weight_boost !== 0 && (
          <span className="text-xs font-mono text-[#475569]">
            +{agent.weight_boost.toFixed(2)}w
          </span>
        )}
      </div>
      <div className="text-xs text-[#94a3b8] mb-1">{agent.expertise}</div>
      <div className="text-xs text-[#475569] italic">{agent.perspective}</div>
    </div>
  );
}
