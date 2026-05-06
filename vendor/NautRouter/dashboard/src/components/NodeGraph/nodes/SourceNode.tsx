import { Handle, Position, type NodeProps } from '@xyflow/react';

interface SourceNodeData {
  label: string;
  isActive: boolean;
}

export function SourceNode({ data }: NodeProps & { data: SourceNodeData }) {
  return (
    <div className={`glass-card px-4 py-3 min-w-[160px] transition-all duration-300 ${data.isActive ? 'ring-2 ring-white animate-pulse-glow' : ''}`}>
      <div className="text-xs text-neutral-500 uppercase tracking-wider mb-1">Incoming</div>
      <div className="text-sm font-medium text-white">{data.label}</div>
      <Handle type="source" position={Position.Right} className="!bg-white !w-2 !h-2 !border-0" />
    </div>
  );
}
