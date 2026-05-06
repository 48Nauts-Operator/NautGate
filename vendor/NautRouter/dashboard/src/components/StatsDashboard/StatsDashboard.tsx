import { useStatsStore } from '../../stores/statsStore';
import { useStats } from '../../hooks/useStats';
import { CostChart } from './CostChart';
import { RequestsChart } from './RequestsChart';
import { LatencyChart } from './LatencyChart';
import { SavingsMetric } from './SavingsMetric';
import { formatCost } from '../../utils/formatters';

const TIME_RANGES = ['1h', '24h', '7d'] as const;

export function StatsDashboard() {
  useStats();

  const stats = useStatsStore((s) => s.stats);
  const timeRange = useStatsStore((s) => s.timeRange);
  const setTimeRange = useStatsStore((s) => s.setTimeRange);
  const loading = useStatsStore((s) => s.loading);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h3 className="text-xs uppercase tracking-wider text-neutral-500">Analytics</h3>
        <div className="flex gap-1">
          {TIME_RANGES.map((r) => (
            <button
              key={r}
              onClick={() => setTimeRange(r)}
              className={`px-2 py-1 text-[10px] rounded font-mono transition-colors ${
                timeRange === r
                  ? 'bg-neutral-800 text-white border border-neutral-700'
                  : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800/50'
              }`}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      {loading && !stats && (
        <div className="glass-card p-8 text-center text-sm text-neutral-600">Loading stats...</div>
      )}

      {stats && (
        <>
          <div className="grid grid-cols-3 gap-5">
            <div className="glass-card p-5">
              <div className="text-[10px] text-neutral-500 uppercase tracking-wider">Total Requests</div>
              <div className="text-2xl font-bold text-white mt-1.5">{stats.total_requests}</div>
            </div>
            <div className="glass-card p-5">
              <div className="text-[10px] text-neutral-500 uppercase tracking-wider">Total Cost</div>
              <div className="text-2xl font-bold text-white mt-1.5">{formatCost(stats.total_cost)}</div>
            </div>
            <div className="glass-card p-5">
              <div className="text-[10px] text-neutral-500 uppercase tracking-wider">Providers</div>
              <div className="text-2xl font-bold text-white mt-1.5">{stats.providers.length}</div>
            </div>
          </div>

          {stats.savings && <SavingsMetric savings={stats.savings} />}

          <div className="grid grid-cols-3 gap-5">
            <div className="glass-card p-5">
              <CostChart providers={stats.providers} />
            </div>
            <div className="glass-card p-5">
              <RequestsChart models={stats.models} />
            </div>
            <div className="glass-card p-5">
              <LatencyChart providers={stats.providers} />
            </div>
          </div>
        </>
      )}

      {!stats && !loading && (
        <div className="glass-card p-8 text-center text-sm text-neutral-600">
          No stats available yet. Send requests to NautRouter to generate data.
        </div>
      )}
    </div>
  );
}
