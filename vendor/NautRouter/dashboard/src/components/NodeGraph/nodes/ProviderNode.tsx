import { Handle, Position, type NodeProps } from '@xyflow/react';
import { getProviderColor, getStatusColor } from '../../../utils/colors';

interface ProviderNodeData {
  label: string;
  providerId: string;
  status: string;
  requestCount: number;
  isSelected: boolean;
}

export function ProviderNode({ data }: NodeProps & { data: ProviderNodeData }) {
  const color = getProviderColor(data.providerId);

  return (
    <div
      className={`glass-card px-4 py-3 min-w-[140px] transition-all duration-300 ${data.isSelected ? 'ring-2 scale-105' : 'opacity-60'}`}
      style={{ borderColor: data.isSelected ? color : undefined, boxShadow: data.isSelected ? `0 0 20px ${color}11` : undefined }}
    >
      <Handle type="target" position={Position.Left} className="!bg-white !w-2 !h-2 !border-0" />

      <div className="flex items-center gap-2 mb-1">
        <div className="w-2 h-2 rounded-full" style={{ backgroundColor: getStatusColor(data.status) }} />
        <span className="text-sm font-medium" style={{ color }}>{data.label}</span>
      </div>
      <div className="text-[10px] text-neutral-500">
        {data.requestCount} requests
      </div>

      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !border-0" style={{ backgroundColor: color }} />
    </div>
  );
}
