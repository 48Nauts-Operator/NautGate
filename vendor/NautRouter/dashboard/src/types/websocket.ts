import type { ScoringDimensions } from './nautRouter';

export type WebSocketEventType =
  | 'connected'
  | 'request_received'
  | 'scoring_complete'
  | 'model_selected'
  | 'response_complete'
  | 'error';

export interface WebSocketEvent {
  type: WebSocketEventType;
  request_id: string;
  timestamp: string;
  data: Record<string, unknown>;
}

export interface RequestReceivedData {
  agent_id: string;
  message_preview: string;
  profile: string;
}

export interface ScoringCompleteData {
  scores: ScoringDimensions;
  complexity_tier: 'simple' | 'medium' | 'complex' | 'reasoning';
  raw_score: number;
  confidence: number;
  signals: string[];
}

export interface ModelSelectedData {
  provider: string;
  model: string;
  reasoning: string;
}

export interface ResponseCompleteData {
  latency_ms: number;
  cost_usd: number;
  tokens_consumed: number;
  success: boolean;
}
