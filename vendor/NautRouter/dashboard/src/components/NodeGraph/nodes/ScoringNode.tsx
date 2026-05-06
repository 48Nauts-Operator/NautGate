import { Handle, Position, type NodeProps } from '@xyflow/react';
import type { ScoringDimensions } from '../../../types/nautRouter';
import { DIMENSION_LABELS } from '../../../types/nautRouter';

interface ScoringNodeData {
  label: string;
  scores: ScoringDimensions | null;
  tier: string;
}

const tierColors: Record<string, string> = {
  simple: '#10B981',
  medium: '#F59E0B',
  complex: '#EF4444',
  reasoning: '#8B5CF6',
};

export function ScoringNode({ data }: NodeProps & { data: ScoringNodeData }) {
  const scores = data.scores;
  const entries = scores
    ? (Object.entries(scores) as [keyof ScoringDimensions, number][]).filter(([k]) => k !== 'overall_confidence')
    : [];

  return (
    <div className="glass-card px-4 py-3 min-w-[220px]">
      <Handle type="target" position={Position.Left} className="!bg-white !w-2 !h-2 !border-0" />

      <div className="flex items-center justify-between mb-2">
        <div className="text-xs text-neutral-500 uppercase tracking-wider">Scoring Engine</div>
        {data.tier && (
          <span
            className="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded"
            style={{ backgroundColor: `${tierColors[data.tier] ?? '#6B7280'}22`, color: tierColors[data.tier] ?? '#6B7280' }}
          >
            {data.tier}
          </span>
        )}
      </div>

      <div className="space-y-1">
        {entries.map(([key, value]) => (
          <div key={key} className="flex items-center gap-2">
            <span className="text-[9px] text-neutral-500 w-16 truncate">{DIMENSION_LABELS[key]?.substring(0, 10)}</span>
            <div className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{
                  width: `${Math.max(2, value * 100)}%`,
                  backgroundColor: value > 0.7 ? '#EF4444' : value > 0.3 ? '#F59E0B' : '#10B981',
                }}
              />
            </div>
          </div>
        ))}
        {entries.length === 0 && (
          <div className="text-[10px] text-neutral-600 text-center py-2">Waiting...</div>
        )}
      </div>

      <Handle type="source" position={Position.Right} className="!bg-white !w-2 !h-2 !border-0" />
    </div>
  );
}
