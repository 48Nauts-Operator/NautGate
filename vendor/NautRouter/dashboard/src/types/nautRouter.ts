export interface ScoringDimensions {
  code_keywords: number;
  reasoning_markers: number;
  creative_markers: number;
  analysis_markers: number;
  math_markers: number;
  length_score: number;
  question_complexity: number;
  context_depth: number;
  instruction_complexity: number;
  multi_step: number;
  domain_specificity: number;
  ambiguity: number;
  formatting_complexity: number;
  overall_confidence: number;
}

export interface RoutingRequest {
  id: string;
  timestamp: Date;
  agent_id: string;
  message_preview: string;
  scores: ScoringDimensions;
  complexity_tier: 'simple' | 'medium' | 'complex' | 'reasoning';
  selected_provider: string;
  selected_model: string;
  latency_ms: number;
  cost_usd: number;
  profile: 'eco' | 'auto' | 'premium';
  status: 'processing' | 'completed' | 'error';
  raw_score?: number;
  confidence?: number;
  signals?: string[];
  reasoning?: string;
  tokens_consumed?: number;
}

export interface Provider {
  id: string;
  name: string;
  status: 'online' | 'offline' | 'degraded';
  models: Model[];
  color: string;
  total_requests: number;
  total_cost: number;
  avg_latency: number;
}

export interface Model {
  id: string;
  name: string;
  cost_per_1k_tokens: number;
  is_local: boolean;
  status: 'available' | 'unavailable';
}

export interface Profile {
  id: 'eco' | 'auto' | 'premium';
  name: string;
  description: string;
  is_active: boolean;
}

export const DIMENSION_LABELS: Record<keyof ScoringDimensions, string> = {
  code_keywords: 'Code Presence',
  reasoning_markers: 'Reasoning',
  creative_markers: 'Creative',
  analysis_markers: 'Technical',
  math_markers: 'Math/Constraints',
  length_score: 'Length',
  question_complexity: 'Questions',
  context_depth: 'Context Depth',
  instruction_complexity: 'Instructions',
  multi_step: 'Multi-step',
  domain_specificity: 'Domain Specific',
  ambiguity: 'Negation/Ambiguity',
  formatting_complexity: 'Format Complexity',
  overall_confidence: 'Confidence',
};
