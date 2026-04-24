import type { AgentPersona } from '@/lib/types';

interface Props {
  agent: AgentPersona;
  index?: number;
  compact?: boolean;
}

const BADGE_COLORS = [
  { bg: 'bg-[#2563eb]/10', text: 'text-[#2563eb]', border: 'border-[#2563eb]/20' },
  { bg: 'bg-[#7c3aed]/10', text: 'text-[#7c3aed]', border: 'border-[#7c3aed]/20' },
  { bg: 'bg-[#16a34a]/10', text: 'text-[#16a34a]', border: 'border-[#16a34a]/20' },
  { bg: 'bg-[#d97706]/10', text: 'text-[#d97706]', border: 'border-[#d97706]/20' },
  { bg: 'bg-[#0891b2]/10', text: 'text-[#0891b2]', border: 'border-[#0891b2]/20' },
  { bg: 'bg-[#ea580c]/10', text: 'text-[#ea580c]', border: 'border-[#ea580c]/20' },
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
          <span className="text-xs font-mono text-[#737373]">
            +{agent.weight_boost.toFixed(2)}w
          </span>
        )}
      </div>
      <div className="text-xs text-[#737373] mb-1">{agent.expertise}</div>
      <div className="text-xs text-[#737373] italic">{agent.perspective}</div>
    </div>
  );
}
