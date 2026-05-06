import { create } from 'zustand';
import type { StatsResponse } from '../types/stats';
import { fetchStats } from '../services/api';

interface StatsState {
  stats: StatsResponse | null;
  timeRange: '1h' | '24h' | '7d';
  loading: boolean;
  error: string | null;
  setTimeRange: (range: '1h' | '24h' | '7d') => void;
  refreshStats: () => Promise<void>;
}

export const useStatsStore = create<StatsState>((set, get) => ({
  stats: null,
  timeRange: '24h',
  loading: false,
  error: null,

  setTimeRange: (range) => {
    set({ timeRange: range });
    get().refreshStats();
  },

  refreshStats: async () => {
    set({ loading: true, error: null });
    try {
      const stats = await fetchStats(get().timeRange);
      set({ stats, loading: false });
    } catch (err) {
      set({ error: (err as Error).message, loading: false });
    }
  },
}));
