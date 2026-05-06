import { create } from 'zustand';
import type { RoutingRequest, ScoringDimensions } from '../types/nautRouter';
import type {
  WebSocketEvent,
  RequestReceivedData,
  ScoringCompleteData,
  ModelSelectedData,
  ResponseCompleteData,
} from '../types/websocket';
import { MAX_FEED_ITEMS } from '../utils/constants';

interface RequestState {
  requests: RoutingRequest[];
  activeRequest: RoutingRequest | null;
  selectedRequest: RoutingRequest | null;
  selectRequest: (request: RoutingRequest | null) => void;
  handleWebSocketEvent: (event: WebSocketEvent) => void;
}

const emptyScores: ScoringDimensions = {
  code_keywords: 0,
  reasoning_markers: 0,
  creative_markers: 0,
  analysis_markers: 0,
  math_markers: 0,
  length_score: 0,
  question_complexity: 0,
  context_depth: 0,
  instruction_complexity: 0,
  multi_step: 0,
  domain_specificity: 0,
  ambiguity: 0,
  formatting_complexity: 0,
  overall_confidence: 0,
};

export const useRequestStore = create<RequestState>((set, get) => ({
  requests: [],
  activeRequest: null,
  selectedRequest: null,

  selectRequest: (request) => set({ selectedRequest: request }),

  handleWebSocketEvent: (event: WebSocketEvent) => {
    const { requests } = get();

    switch (event.type) {
      case 'request_received': {
        const data = event.data as unknown as RequestReceivedData;
        const newRequest: RoutingRequest = {
          id: event.request_id,
          timestamp: new Date(event.timestamp),
          agent_id: data.agent_id,
          message_preview: data.message_preview,
          scores: { ...emptyScores },
          complexity_tier: 'simple',
          selected_provider: '',
          selected_model: '',
          latency_ms: 0,
          cost_usd: 0,
          profile: data.profile as RoutingRequest['profile'],
          status: 'processing',
        };
        const updated = [newRequest, ...requests].slice(0, MAX_FEED_ITEMS);
        set({ requests: updated, activeRequest: newRequest });
        break;
      }
      case 'scoring_complete': {
        const data = event.data as unknown as ScoringCompleteData;
        set({
          requests: requests.map((r) =>
            r.id === event.request_id
              ? {
                  ...r,
                  scores: data.scores,
                  complexity_tier: data.complexity_tier,
                  raw_score: data.raw_score,
                  confidence: data.confidence,
                  signals: data.signals,
                }
              : r,
          ),
        });
        break;
      }
      case 'model_selected': {
        const data = event.data as unknown as ModelSelectedData;
        set({
          requests: requests.map((r) =>
            r.id === event.request_id
              ? {
                  ...r,
                  selected_provider: data.provider,
                  selected_model: data.model,
                  reasoning: data.reasoning,
                }
              : r,
          ),
        });
        break;
      }
      case 'response_complete': {
        const data = event.data as unknown as ResponseCompleteData;
        set({
          requests: requests.map((r) =>
            r.id === event.request_id
              ? {
                  ...r,
                  latency_ms: data.latency_ms,
                  cost_usd: data.cost_usd,
                  tokens_consumed: data.tokens_consumed,
                  status: data.success ? 'completed' : 'error',
                }
              : r,
          ),
          activeRequest: null,
        });
        break;
      }
      case 'error': {
        set({
          requests: requests.map((r) =>
            r.id === event.request_id ? { ...r, status: 'error' as const } : r,
          ),
          activeRequest: null,
        });
        break;
      }
    }
  },
}));
