export const colors = {
  providers: {
    anthropic: '#4F46E5',
    lmstudio: '#10B981',
    gemini: '#F59E0B',
  } as Record<string, string>,
  complexity: {
    simple: '#10B981',
    medium: '#F59E0B',
    complex: '#EF4444',
    reasoning: '#8B5CF6',
  } as Record<string, string>,
  status: {
    online: '#10B981',
    degraded: '#F59E0B',
    offline: '#EF4444',
    processing: '#8B5CF6',
  } as Record<string, string>,
};

export function getProviderColor(provider: string): string {
  return colors.providers[provider] ?? '#6B7280';
}

export function getTierColor(tier: string): string {
  return colors.complexity[tier.toLowerCase()] ?? '#6B7280';
}

export function getStatusColor(status: string): string {
  return colors.status[status] ?? '#6B7280';
}
