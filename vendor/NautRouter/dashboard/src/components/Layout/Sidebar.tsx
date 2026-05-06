import { useConfigStore } from '../../stores/configStore';
import { useRequestStore } from '../../stores/requestStore';
import { getProviderColor, getStatusColor } from '../../utils/colors';
import { formatCost } from '../../utils/formatters';

export function Sidebar() {
  const providers = useConfigStore((s) => s.providers);
  const requestCount = useRequestStore((s) => s.requests.length);

  return (
    <aside className="glass-card w-56 shrink-0 flex flex-col gap-5 p-5 overflow-y-auto">
      <div>
        <nav className="mb-5">
          <h3 className="text-[10px] uppercase tracking-widest text-neutral-600 mb-3 px-1">Navigation</h3>
          <div className="flex flex-col gap-1">
            <button className="w-full text-left text-sm text-white bg-neutral-800/60 px-3 py-2.5 rounded-lg font-medium">
              Overview
            </button>
          </div>
        </nav>
        <h3 className="text-[10px] uppercase tracking-widest text-neutral-600 mb-3 px-1">Providers</h3>
        <div className="flex flex-col gap-2.5">
          {providers.map((p) => (
            <div key={p.id} className="glass-card p-3.5 bg-neutral-900/50 border-neutral-800">
              <div className="flex items-center gap-2 mb-1">
                <div className="w-2 h-2 rounded-full" style={{ backgroundColor: getStatusColor(p.status) }} />
                <span className="text-sm font-medium" style={{ color: getProviderColor(p.id) }}>
                  {p.name}
                </span>
              </div>
              <div className="text-xs text-neutral-500 space-y-0.5">
                <div className="flex justify-between">
                  <span>Requests</span>
                  <span className="text-neutral-300">{p.total_requests}</span>
                </div>
                <div className="flex justify-between">
                  <span>Cost</span>
                  <span className="text-neutral-300">{formatCost(p.total_cost)}</span>
                </div>
                <div className="flex justify-between">
                  <span>Latency</span>
                  <span className="text-neutral-300">{p.avg_latency}ms</span>
                </div>
              </div>
            </div>
          ))}
          {providers.length === 0 && (
            <div className="text-xs text-neutral-600 text-center py-4">
              Waiting for data...
            </div>
          )}
        </div>
      </div>

      <div className="mt-auto pt-4 border-t border-neutral-800">
        <div className="text-xs text-neutral-500 space-y-1">
          <div className="flex justify-between">
            <span>Tracked</span>
            <span className="text-neutral-300">{requestCount} req</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
