import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts';
import { colors } from '../../utils/colors';
import type { ProviderStats } from '../../types/stats';

interface CostChartProps {
  providers: ProviderStats[];
}

export function CostChart({ providers }: CostChartProps) {
  const data = providers
    .filter((p) => p.total_cost > 0)
    .map((p) => ({
      name: p.provider_id,
      value: Number(p.total_cost.toFixed(4)),
      color: colors.providers[p.provider_id] ?? '#6B7280',
    }));

  if (data.length === 0) {
    return <div className="flex items-center justify-center h-full text-xs text-neutral-600">No cost data</div>;
  }

  return (
    <div className="h-full">
      <h4 className="text-[10px] uppercase tracking-wider text-neutral-500 mb-2">Cost by Provider</h4>
      <ResponsiveContainer width="100%" height={160}>
        <PieChart>
          <Pie data={data} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={35} outerRadius={55} paddingAngle={3}>
            {data.map((entry, i) => (
              <Cell key={i} fill={entry.color} stroke="transparent" />
            ))}
          </Pie>
          <Tooltip
            contentStyle={{ background: '#171717', border: '1px solid #262626', borderRadius: '8px', fontSize: '11px', color: '#e5e5e5' }}
            formatter={(value) => [`$${Number(value).toFixed(4)}`, 'Cost']}
          />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}
