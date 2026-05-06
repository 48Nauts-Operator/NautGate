import { BaseEdge, getSmoothStepPath, type EdgeProps } from '@xyflow/react';

export function AnimatedEdge(props: EdgeProps) {
  const { sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data } = props;
  const isActive = (data as Record<string, unknown>)?.active === true;

  const [edgePath] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 8,
  });

  return (
    <BaseEdge
      path={edgePath}
      style={{
        stroke: isActive ? '#ffffff' : '#404040',
        strokeWidth: isActive ? 2 : 1,
        strokeDasharray: isActive ? '8 4' : undefined,
        animation: isActive ? 'flow-edge 1s linear infinite' : undefined,
        transition: 'stroke 0.3s, stroke-width 0.3s',
      }}
    />
  );
}
