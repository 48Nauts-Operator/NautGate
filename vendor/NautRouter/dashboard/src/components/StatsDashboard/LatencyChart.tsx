import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Tooltip, Cell } from 'recharts';
import { colors } from '../../utils/colors';
import type { ProviderStats } from '../../types/stats';

interface LatencyChartProps {
  providers: ProviderStats[];
}

export function LatencyChart({ providers }: LatencyChartProps) {
  const data = providers.map((p) => ({
    name: p.provider_id,
    latency: p.avg_latency,
    color: colors.providers[p.provider_id] ?? '#6B7280',
  }));

  if (data.length === 0) {
    return <div className="flex items-center justify-center h-full text-xs text-neutral-600">No latency data</div>;
  }

  return (
    <div className="h-full">
      <h4 className="text-[10px] uppercase tracking-wider text-neutral-500 mb-2">Avg Latency by Provider</h4>
      <ResponsiveContainer width="100%" height={160}>
        <BarChart data={data} margin={{ left: 10 }}>
          <XAxis dataKey="name" tick={{ fontSize: 10, fill: '#737373' }} />
          <YAxis tick={{ fontSize: 10, fill: '#525252' }} />
          <Tooltip
            contentStyle={{ background: '#171717', border: '1px solid #262626', borderRadius: '8px', fontSize: '11px', color: '#e5e5e5' }}
            formatter={(value) => [`${value}ms`, 'Latency']}
          />
          <Bar dataKey="latency" radius={[4, 4, 0, 0]}>
            {data.map((entry, i) => (
              <Cell key={i} fill={entry.color} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
