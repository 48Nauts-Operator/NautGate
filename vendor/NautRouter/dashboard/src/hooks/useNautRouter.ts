import { useEffect } from 'react';
import { useConfigStore } from '../stores/configStore';

export function useNautRouter() {
  const fetchConfig = useConfigStore((s) => s.fetchConfig);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);
}
