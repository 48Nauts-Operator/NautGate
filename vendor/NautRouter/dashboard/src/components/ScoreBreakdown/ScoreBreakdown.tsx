import { useRequestStore } from '../../stores/requestStore';
import { DIMENSION_LABELS, type ScoringDimensions } from '../../types/nautRouter';
import { DimensionBar } from './DimensionBar';
import { getTierColor, getProviderColor } from '../../utils/colors';
import { formatCost, formatLatency, formatTokens } from '../../utils/formatters';

export function ScoreBreakdown() {
  const selectedRequest = useRequestStore((s) => s.selectedRequest);
  const selectRequest = useRequestStore((s) => s.selectRequest);

  if (!selectedRequest) {
    return (
    <div className="glass-card p-4 flex items-center justify-center text-sm text-neutral-600 h-48">
      Select a request from the feed to view score breakdown
    </div>
  );
}

const scores = selectedRequest.scores;
const entries = Object.entries(scores) as [keyof ScoringDimensions, number][];
const tierColor = getTierColor(selectedRequest.complexity_tier);
const providerColor = getProviderColor(selectedRequest.selected_provider);

return (
  <div className="glass-card p-4 flex flex-col h-full overflow-y-auto bg-neutral-900/50 border-neutral-800">
    <div className="flex items-center justify-between mb-3">
      <h3 className="text-xs uppercase tracking-wider text-neutral-500">Score Breakdown</h3>
      <button onClick={() => selectRequest(null)} className="text-xs text-neutral-600 hover:text-neutral-400">
        &times;
      </button>
    </div>

    <div className="flex items-center gap-3 mb-4">
      <span
        className="text-xs font-bold uppercase px-2 py-0.5 rounded"
        style={{ backgroundColor: `${tierColor}22`, color: tierColor }}
      >
        {selectedRequest.complexity_tier}
      </span>
      {selectedRequest.selected_model && (
        <span className="text-xs font-mono" style={{ color: providerColor }}>
          {selectedRequest.selected_model}
        </span>
      )}
      {selectedRequest.status === 'completed' && (
        <div className="flex items-center gap-2 ml-auto text-[10px] text-neutral-500">
          <span>{formatLatency(selectedRequest.latency_ms)}</span>
          <span>{formatCost(selectedRequest.cost_usd)}</span>
          {selectedRequest.tokens_consumed != null && (
            <span>{formatTokens(selectedRequest.tokens_consumed)} tok</span>
          )}
        </div>
      )}
    </div>

    {selectedRequest.reasoning && (
      <div className="text-[10px] text-neutral-500 mb-3 font-mono bg-neutral-900/50 p-2 rounded border border-neutral-800">
        {selectedRequest.reasoning}
      </div>
    )}

    <div className="space-y-2 flex-1">
      {entries.map(([key, value]) => (
        <DimensionBar key={key} label={DIMENSION_LABELS[key]} value={value} />
      ))}
    </div>

    {selectedRequest.signals && selectedRequest.signals.length > 0 && (
      <div className="mt-3 pt-3 border-t border-neutral-800/50">
        <div className="text-[10px] text-neutral-500 mb-1">Signals</div>
        <div className="flex flex-wrap gap-1">
          {selectedRequest.signals.map((signal, i) => (
            <span key={i} className="text-[9px] bg-neutral-800/60 text-neutral-400 px-1.5 py-0.5 rounded border border-neutral-800">
              {signal}
            </span>
          ))}
        </div>
      </div>
    )}
  </div>
);
}
