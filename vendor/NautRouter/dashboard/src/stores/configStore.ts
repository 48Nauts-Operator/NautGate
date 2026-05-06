import { create } from 'zustand';
import type { Provider } from '../types/nautRouter';
import { fetchProfile, updateProfile, fetchProviders } from '../services/api';

interface ConfigState {
  currentProfile: string;
  availableProfiles: string[];
  providers: Provider[];
  loading: boolean;
  connectionStatus: 'connecting' | 'connected' | 'disconnected';
  setConnectionStatus: (status: ConfigState['connectionStatus']) => void;
  fetchConfig: () => Promise<void>;
  changeProfile: (profile: string) => Promise<void>;
}

export const useConfigStore = create<ConfigState>((set) => ({
  currentProfile: 'auto',
  availableProfiles: ['eco', 'auto', 'premium'],
  providers: [],
  loading: false,
  connectionStatus: 'disconnected',

  setConnectionStatus: (status) => set({ connectionStatus: status }),

  fetchConfig: async () => {
    set({ loading: true });
    try {
      const [profileData, providersData] = await Promise.all([
        fetchProfile(),
        fetchProviders(),
      ]);
      set({
        currentProfile: profileData.current,
        availableProfiles: profileData.available,
        providers: providersData.providers,
        loading: false,
      });
    } catch {
      set({ loading: false });
    }
  },

  changeProfile: async (profile) => {
    try {
      await updateProfile(profile);
      set({ currentProfile: profile });
    } catch {
      /* silent — profile change failed */
    }
  },
}));
