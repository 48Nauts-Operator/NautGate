import { formatCost, formatPercentage } from '../../utils/formatters';
import type { SavingsMetric as SavingsMetricType } from '../../types/stats';

interface SavingsMetricProps {
  savings: SavingsMetricType;
}

export function SavingsMetric({ savings }: SavingsMetricProps) {
  return (
    <div className="glass-card p-4 bg-neutral-900/50 border-neutral-800">
      <h4 className="text-[10px] uppercase tracking-wider text-neutral-500 mb-3">Cost Savings vs Always-Opus</h4>
      <div className="flex items-end gap-4">
        <div>
          <div className="text-3xl font-bold text-emerald-500">
            {formatPercentage(savings.savings_percentage)}
          </div>
          <div className="text-xs text-neutral-500 mt-1">saved</div>
        </div>
        <div className="flex-1 space-y-1.5 text-xs">
          <div className="flex justify-between text-neutral-400">
            <span>Actual cost</span>
            <span className="text-white font-mono">{formatCost(savings.actual_cost)}</span>
          </div>
          <div className="flex justify-between text-neutral-400">
            <span>Opus baseline</span>
            <span className="text-neutral-500 font-mono">{formatCost(savings.opus_baseline_cost)}</span>
          </div>
          <div className="flex justify-between text-emerald-500">
            <span>Saved</span>
            <span className="font-mono font-bold">{formatCost(savings.savings_usd)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
