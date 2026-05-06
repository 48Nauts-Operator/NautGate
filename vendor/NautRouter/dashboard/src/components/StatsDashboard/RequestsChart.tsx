import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Tooltip, Cell } from 'recharts';
import { colors } from '../../utils/colors';
import type { ModelStats } from '../../types/stats';

interface RequestsChartProps {
  models: ModelStats[];
}

export function RequestsChart({ models }: RequestsChartProps) {
  const data = models.map((m) => ({
    name: m.model_id.length > 12 ? m.model_id.substring(0, 12) + '...' : m.model_id,
    requests: m.request_count,
    color: colors.providers[m.provider_id] ?? '#6B7280',
  }));

  if (data.length === 0) {
    return <div className="flex items-center justify-center h-full text-xs text-neutral-600">No request data</div>;
  }

  return (
    <div className="h-full">
      <h4 className="text-[10px] uppercase tracking-wider text-neutral-500 mb-2">Requests by Model</h4>
      <ResponsiveContainer width="100%" height={160}>
        <BarChart data={data} layout="vertical" margin={{ left: 60 }}>
          <XAxis type="number" tick={{ fontSize: 10, fill: '#525252' }} />
          <YAxis type="category" dataKey="name" tick={{ fontSize: 9, fill: '#737373' }} width={60} />
          <Tooltip
            contentStyle={{ background: '#171717', border: '1px solid #262626', borderRadius: '8px', fontSize: '11px', color: '#e5e5e5' }}
          />
          <Bar dataKey="requests" radius={[0, 4, 4, 0]}>
            {data.map((entry, i) => (
              <Cell key={i} fill={entry.color} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
