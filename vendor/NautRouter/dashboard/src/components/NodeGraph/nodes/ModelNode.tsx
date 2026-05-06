import { Handle, Position, type NodeProps } from '@xyflow/react';
import { getProviderColor } from '../../../utils/colors';

interface ModelNodeData {
  label: string;
  providerId: string;
  isLocal: boolean;
  isSelected: boolean;
}

export function ModelNode({ data }: NodeProps & { data: ModelNodeData }) {
  const color = getProviderColor(data.providerId);

  return (
    <div
      className={`glass-card px-3 py-2 min-w-[120px] transition-all duration-300 ${data.isSelected ? 'ring-1 scale-105' : 'opacity-40'}`}
      style={{ borderColor: data.isSelected ? color : undefined }}
    >
      <Handle type="target" position={Position.Left} className="!w-1.5 !h-1.5 !border-0" style={{ backgroundColor: color }} />

      <div className="flex items-center gap-1.5">
        {data.isLocal && <span className="text-[8px] bg-emerald-900/50 text-emerald-400 px-1 rounded">LOCAL</span>}
        <span className={`text-xs ${data.isSelected ? 'text-white font-medium' : 'text-slate-500'}`}>
          {data.label}
        </span>
      </div>
    </div>
  );
}
