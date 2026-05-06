import axios from 'axios';
import { API_BASE } from '../utils/constants';
import type { StatsResponse } from '../types/stats';
import type { Provider } from '../types/nautRouter';

const client = axios.create({ baseURL: API_BASE });

export async function fetchProfile(): Promise<{ current: string; available: string[] }> {
  const { data } = await client.get('/v1/profile');
  return data;
}

export async function updateProfile(profile: string): Promise<void> {
  await client.put('/v1/profile', { profile });
}

export async function fetchProviders(): Promise<{ providers: Provider[] }> {
  const { data } = await client.get('/v1/providers');
  return data;
}

export async function fetchStats(range: string): Promise<StatsResponse> {
  const { data } = await client.get(`/v1/stats?range=${range}`);
  return data;
}
