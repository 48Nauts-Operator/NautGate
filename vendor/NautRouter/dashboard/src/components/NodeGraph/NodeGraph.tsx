import { useMemo } from 'react';
import { ReactFlow, type Node, type Edge } from '@xyflow/react';
import { useRequestStore } from '../../stores/requestStore';
import { useConfigStore } from '../../stores/configStore';
import { SourceNode } from './nodes/SourceNode';
import { ScoringNode } from './nodes/ScoringNode';
import { ProviderNode } from './nodes/ProviderNode';
import { ModelNode } from './nodes/ModelNode';
import { AnimatedEdge } from './edges/AnimatedEdge';

const nodeTypes = {
  source: SourceNode,
  scoring: ScoringNode,
  provider: ProviderNode,
  model: ModelNode,
};

const edgeTypes = {
  animated: AnimatedEdge,
};

export function NodeGraph() {
  const activeRequest = useRequestStore((s) => s.activeRequest);
  const selectedRequest = useRequestStore((s) => s.selectedRequest);
  const providers = useConfigStore((s) => s.providers);

  const displayRequest = activeRequest ?? selectedRequest;
  const selectedProvider = displayRequest?.selected_provider ?? '';
  const selectedModel = displayRequest?.selected_model ?? '';

  const { nodes, edges } = useMemo(() => {
    const n: Node[] = [
      {
        id: 'source',
        type: 'source',
        position: { x: 0, y: 100 },
        data: {
          label: displayRequest?.message_preview?.substring(0, 40) ?? 'Waiting for request...',
          isActive: !!activeRequest,
        },
      },
      {
        id: 'scoring',
        type: 'scoring',
        position: { x: 220, y: 20 },
        data: {
          label: 'Scoring Engine',
          scores: displayRequest?.scores ?? null,
          tier: displayRequest?.complexity_tier ?? '',
        },
      },
    ];

    const e: Edge[] = [
      { id: 'e-source-scoring', source: 'source', target: 'scoring', type: 'animated', data: { active: !!activeRequest } },
    ];

    const providerList = providers.length > 0
      ? providers
      : [
          { id: 'anthropic', name: 'Anthropic', status: 'online' as const, models: [], color: '#4F46E5', total_requests: 0, total_cost: 0, avg_latency: 0 },
          { id: 'lmstudio', name: 'LM Studio', status: 'online' as const, models: [], color: '#10B981', total_requests: 0, total_cost: 0, avg_latency: 0 },
          { id: 'gemini', name: 'Gemini', status: 'online' as const, models: [], color: '#F59E0B', total_requests: 0, total_cost: 0, avg_latency: 0 },
        ];

    providerList.forEach((p, i) => {
      const yBase = i * 90 + 5;
      const provNodeId = `provider-${p.id}`;

      n.push({
        id: provNodeId,
        type: 'provider',
        position: { x: 480, y: yBase },
        data: {
          label: p.name,
          providerId: p.id,
          status: p.status,
          requestCount: p.total_requests,
          isSelected: selectedProvider === p.id,
        },
      });

      e.push({
        id: `e-scoring-${p.id}`,
        source: 'scoring',
        target: provNodeId,
        type: 'animated',
        data: { active: selectedProvider === p.id && !!displayRequest },
      });

      p.models.forEach((m, mi) => {
        const modelNodeId = `model-${m.id}`;
        n.push({
          id: modelNodeId,
          type: 'model',
          position: { x: 660, y: yBase + mi * 36 },
          data: {
            label: m.id,
            providerId: p.id,
            isLocal: m.is_local,
            isSelected: selectedModel === m.id,
          },
        });

        e.push({
          id: `e-${p.id}-${m.id}`,
          source: provNodeId,
          target: modelNodeId,
          type: 'animated',
          data: { active: selectedModel === m.id && !!displayRequest },
        });
      });
    });

    return { nodes: n, edges: e };
  }, [displayRequest, activeRequest, selectedProvider, selectedModel, providers]);

  return (
    <div className="glass-card h-[300px] overflow-hidden shrink-0">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        proOptions={{ hideAttribution: true }}
        panOnDrag={false}
        zoomOnScroll={false}
        zoomOnPinch={false}
        zoomOnDoubleClick={false}
        nodesDraggable={false}
        nodesConnectable={false}
        className="bg-transparent"
      />
    </div>
  );
}
