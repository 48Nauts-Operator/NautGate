interface DimensionBarProps {
  label: string;
  value: number;
  maxValue?: number;
}

export function DimensionBar({ label, value, maxValue = 1 }: DimensionBarProps) {
  const pct = Math.min(100, (value / maxValue) * 100);
  const color = pct > 70 ? '#EF4444' : pct > 30 ? '#F59E0B' : '#10B981';

  return (
    <div className="flex items-center gap-3">
      <span className="text-xs text-neutral-400 w-28 truncate" title={label}>{label}</span>
      <div className="flex-1 h-2 bg-neutral-800 rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-700 ease-out"
          style={{ width: `${Math.max(1, pct)}%`, backgroundColor: color }}
        />
      </div>
      <span className="text-[10px] text-neutral-500 w-8 text-right font-mono">{value.toFixed(2)}</span>
    </div>
  );
}
