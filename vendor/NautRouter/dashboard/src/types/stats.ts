export interface TimeSeriesPoint {
  timestamp: Date;
  value: number;
}

export interface ProviderStats {
  provider_id: string;
  total_requests: number;
  total_cost: number;
  avg_latency: number;
  success_rate: number;
  cost_trend: TimeSeriesPoint[];
  request_trend: TimeSeriesPoint[];
}

export interface ModelStats {
  model_id: string;
  provider_id: string;
  request_count: number;
  avg_cost: number;
  avg_latency: number;
}

export interface SavingsMetric {
  actual_cost: number;
  opus_baseline_cost: number;
  savings_usd: number;
  savings_percentage: number;
}

export interface StatsResponse {
  time_range: string;
  total_requests: number;
  total_cost: number;
  providers: ProviderStats[];
  models: ModelStats[];
  savings: SavingsMetric;
  profile_distribution: Record<string, number>;
}
