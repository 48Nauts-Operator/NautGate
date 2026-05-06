import { useEffect } from 'react';
import { useRequestStore } from '../stores/requestStore';
import { useStatsStore } from '../stores/statsStore';
import { useConfigStore } from '../stores/configStore';
import { MOCK_PROVIDERS, MOCK_REQUESTS, MOCK_STATS } from '../utils/mockData';

export function useMockData(enabled: boolean) {
  const setConnectionStatus = useConfigStore((s) => s.setConnectionStatus);

  useEffect(() => {
    if (!enabled) return;

    useConfigStore.setState({
      currentProfile: 'auto',
      availableProfiles: ['eco', 'auto', 'premium'],
      providers: MOCK_PROVIDERS,
      connectionStatus: 'connected',
    });

    useRequestStore.setState({
      requests: MOCK_REQUESTS.filter((r) => r.status !== 'processing'),
      activeRequest: MOCK_REQUESTS.find((r) => r.status === 'processing') ?? null,
      selectedRequest: MOCK_REQUESTS[0],
    });

    useStatsStore.setState({
      stats: MOCK_STATS,
      timeRange: '24h',
      loading: false,
      error: null,
    });

    setConnectionStatus('connected');
  }, [enabled, setConnectionStatus]);
}
