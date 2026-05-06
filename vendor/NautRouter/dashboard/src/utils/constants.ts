export const WS_URL = import.meta.env.VITE_NAUTROUTER_WS_URL ?? 'ws://localhost:8403';
export const API_BASE = import.meta.env.VITE_NAUTROUTER_HTTP_URL ?? '';

export const PROFILE_DESCRIPTIONS: Record<string, { name: string; description: string }> = {
  eco: {
    name: 'Eco',
    description: 'Local LM Studio first, Gemini Flash fallback. Minimum cost.',
  },
  auto: {
    name: 'Auto',
    description: 'Balanced routing — local for simple, Sonnet for complex.',
  },
  premium: {
    name: 'Premium',
    description: 'Opus for complex, Sonnet for medium. Maximum quality.',
  },
};

export const MAX_FEED_ITEMS = 200;
export const WS_RECONNECT_INTERVAL = 3000;
export const STATS_REFRESH_INTERVAL = 30_000;
