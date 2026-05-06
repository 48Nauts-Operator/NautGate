import type { RoutingRequest } from '../../types/nautRouter';
import { getProviderColor, getTierColor } from '../../utils/colors';
import { formatCost, formatLatency, timeAgo } from '../../utils/formatters';

interface RequestItemProps {
  request: RoutingRequest;
  isSelected: boolean;
  onClick: () => void;
  style?: React.CSSProperties;
}

export function RequestItem({ request, isSelected, onClick, style }: RequestItemProps) {
  const tierColor = getTierColor(request.complexity_tier);
  const providerColor = getProviderColor(request.selected_provider);

  return (
    <div
      style={style}
      onClick={onClick}
      className={`px-3 py-2 cursor-pointer border-b border-neutral-800 transition-colors hover:bg-neutral-800/30 ${isSelected ? 'bg-neutral-800/50 border-l-2 border-l-white' : ''}`}
    >
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-neutral-500">{request.agent_id}</span>
          <span
            className="text-[9px] font-bold uppercase px-1 py-0.5 rounded"
            style={{ backgroundColor: `${tierColor}22`, color: tierColor }}
          >
            {request.complexity_tier}
          </span>
        </div>
        <span className="text-[10px] text-neutral-600">{timeAgo(request.timestamp)}</span>
      </div>

      <div className="text-xs text-neutral-300 truncate mb-1">{request.message_preview || '...'}</div>

      <div className="flex items-center gap-3 text-[10px] text-neutral-500">
        {request.selected_model && (
          <span style={{ color: providerColor }}>{request.selected_model}</span>
        )}
        {request.status === 'completed' && (
          <>
            <span>{formatLatency(request.latency_ms)}</span>
            <span>{formatCost(request.cost_usd)}</span>
          </>
        )}
        {request.status === 'processing' && (
          <span className="text-neutral-400">processing...</span>
        )}
        {request.status === 'error' && (
          <span className="text-[var(--color-status-offline)]">error</span>
        )}
      </div>
    </div>
  );
}
