import { useEffect } from 'react';
import { useStatsStore } from '../stores/statsStore';
import { STATS_REFRESH_INTERVAL } from '../utils/constants';

export function useStats() {
  const refreshStats = useStatsStore((s) => s.refreshStats);

  useEffect(() => {
    refreshStats();
    const interval = setInterval(refreshStats, STATS_REFRESH_INTERVAL);
    return () => clearInterval(interval);
  }, [refreshStats]);
}
