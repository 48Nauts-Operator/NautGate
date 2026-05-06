# NautRouter Dashboard - Developer Specification

## Overview

A real-time AI model routing visualizer that provides live insights into NautRouter's decision-making process. Think RabbitMQ Management UI meets an audio mixing board — watch requests flow through the scoring engine and see which models get selected in real-time.

**NautRouter Context:**
- Location: `/Users/jarvis/clawd/projects/naut-router/`
- Port: 8402
- 14-dimension scoring engine for request complexity analysis
- 3 providers: Anthropic, LM Studio (local), Google Gemini
- 3 profiles: eco (local-first), auto (smart routing), premium (tiered quality)
- Cost logging to memory API: `http://100.71.163.122:8085/memories`

## Technology Stack

- **Frontend:** React 18+ with TypeScript
- **Build Tool:** Vite
- **Styling:** TailwindCSS + HeadlessUI
- **Visualization:** React Flow for node graphs, Recharts for analytics
- **Real-time:** WebSocket connection to NautRouter
- **State:** Zustand for client state management
- **HTTP Client:** Axios

## File Structure

```
naut-router-dashboard/
├── src/
│   ├── components/
│   │   ├── NodeGraph/
│   │   │   ├── NodeGraph.tsx
│   │   │   ├── nodes/
│   │   │   │   ├── SourceNode.tsx
│   │   │   │   ├── ScoringNode.tsx
│   │   │   │   ├── ProviderNode.tsx
│   │   │   │   └── ModelNode.tsx
│   │   │   └── edges/
│   │   │       └── AnimatedEdge.tsx
│   │   ├── RequestFeed/
│   │   │   ├── RequestFeed.tsx
│   │   │   └── RequestItem.tsx
│   │   ├── ScoreBreakdown/
│   │   │   ├── ScoreBreakdown.tsx
│   │   │   └── DimensionBar.tsx
│   │   ├── StatsDashboard/
│   │   │   ├── StatsDashboard.tsx
│   │   │   ├── CostChart.tsx
│   │   │   ├── RequestsChart.tsx
│   │   │   ├── LatencyChart.tsx
│   │   │   └── SavingsMetric.tsx
│   │   ├── ProfileSelector/
│   │   │   └── ProfileSelector.tsx
│   │   └── Layout/
│   │       ├── Header.tsx
│   │       ├── Sidebar.tsx
│   │       └── Layout.tsx
│   ├── hooks/
│   │   ├── useWebSocket.ts
│   │   ├── useNautRouter.ts
│   │   └── useStats.ts
│   ├── stores/
│   │   ├── requestStore.ts
│   │   ├── statsStore.ts
│   │   └── configStore.ts
│   ├── types/
│   │   ├── nautRouter.ts
│   │   ├── websocket.ts
│   │   └── stats.ts
│   ├── utils/
│   │   ├── formatters.ts
│   │   ├── colors.ts
│   │   └── constants.ts
│   ├── services/
│   │   └── api.ts
│   ├── App.tsx
│   └── main.tsx
├── public/
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── tsconfig.json
└── README.md
```

## Data Models

### Core Types

```typescript
// types/nautRouter.ts
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
  complexity_tier: 'simple' | 'medium' | 'complex';
  selected_provider: string;
  selected_model: string;
  latency_ms: number;
  cost_usd: number;
  profile: 'eco' | 'auto' | 'premium';
  status: 'processing' | 'completed' | 'error';
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
  provider_id: string;
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
```

### WebSocket Events

```typescript
// types/websocket.ts
export interface WebSocketEvent {
  type: 'request_received' | 'scoring_complete' | 'model_selected' | 'response_complete' | 'error';
  request_id: string;
  timestamp: string;
  data: any;
}

export interface RequestReceivedEvent extends WebSocketEvent {
  type: 'request_received';
  data: {
    agent_id: string;
    message_preview: string;
    profile: string;
  };
}

export interface ScoringCompleteEvent extends WebSocketEvent {
  type: 'scoring_complete';
  data: {
    scores: ScoringDimensions;
    complexity_tier: 'simple' | 'medium' | 'complex';
  };
}

export interface ModelSelectedEvent extends WebSocketEvent {
  type: 'model_selected';
  data: {
    provider: string;
    model: string;
    reasoning: string;
  };
}

export interface ResponseCompleteEvent extends WebSocketEvent {
  type: 'response_complete';
  data: {
    latency_ms: number;
    cost_usd: number;
    tokens_consumed: number;
    success: boolean;
  };
}
```

### Statistics Models

```typescript
// types/stats.ts
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
  profile_distribution: { [key: string]: number };
}
```

## Component Specifications

### 1. NodeGraph Component

**Purpose:** Visual flow representation using React Flow

**Features:**
- Source node ("Incoming Request")
- Central scoring node with 14-dimension equalizer bars
- Provider nodes with model children
- Animated edges that light up during routing
- Color-coded by provider (green=local, blue=Anthropic, orange=Gemini)

**Props:**
```typescript
interface NodeGraphProps {
  activeRequest?: RoutingRequest;
  providers: Provider[];
  className?: string;
}
```

**Node Types:**
- `SourceNode`: Entry point, pulses when new request arrives
- `ScoringNode`: Shows live dimension scores as animated bars
- `ProviderNode`: Provider status indicator
- `ModelNode`: Individual model availability/usage

### 2. RequestFeed Component

**Purpose:** Live scrollable feed of routing decisions

**Features:**
- WebSocket-powered real-time updates
- Virtualized scrolling for performance
- Click to select request for score breakdown
- Color-coded status indicators

**Props:**
```typescript
interface RequestFeedProps {
  requests: RoutingRequest[];
  selectedRequest?: RoutingRequest;
  onRequestSelect: (request: RoutingRequest) => void;
  className?: string;
}
```

### 3. ScoreBreakdown Component

**Purpose:** Detailed view of scoring dimensions for selected request

**Features:**
- 14 horizontal progress bars
- Tier boundary indicators (simple/medium/complex thresholds)
- Animated transitions when switching requests
- Tooltips explaining each dimension

**Props:**
```typescript
interface ScoreBreakdownProps {
  request?: RoutingRequest;
  onClose: () => void;
}
```

### 4. StatsDashboard Component

**Purpose:** Historical analytics and metrics

**Features:**
- Time range selector (1h, 24h, 7d)
- Cost distribution pie chart
- Requests per model bar chart
- Latency trends line chart
- Cost savings metric vs "always Opus"

**Props:**
```typescript
interface StatsDashboardProps {
  timeRange: '1h' | '24h' | '7d';
  onTimeRangeChange: (range: string) => void;
}
```

### 5. ProfileSelector Component

**Purpose:** Switch between eco/auto/premium routing profiles

**Features:**
- Radio button group with profile descriptions
- Visual indicator of current active profile
- PUT request to NautRouter on change

**Props:**
```typescript
interface ProfileSelectorProps {
  currentProfile: Profile;
  profiles: Profile[];
  onProfileChange: (profileId: string) => void;
}
```

## WebSocket Integration

### Connection Management

```typescript
// hooks/useWebSocket.ts
export const useWebSocket = (url: string) => {
  const [socket, setSocket] = useState<WebSocket | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<'connecting' | 'connected' | 'disconnected'>('disconnected');
  
  const connect = useCallback(() => {
    const ws = new WebSocket(url);
    
    ws.onopen = () => setConnectionStatus('connected');
    ws.onclose = () => setConnectionStatus('disconnected');
    ws.onerror = () => setConnectionStatus('disconnected');
    
    ws.onmessage = (event) => {
      const data: WebSocketEvent = JSON.parse(event.data);
      // Handle different event types
    };
    
    setSocket(ws);
  }, [url]);
  
  // Auto-reconnect logic, cleanup, etc.
};
```

### Event Handling

```typescript
// stores/requestStore.ts
interface RequestState {
  requests: RoutingRequest[];
  activeRequest?: RoutingRequest;
  selectedRequest?: RoutingRequest;
}

export const useRequestStore = create<RequestState>((set, get) => ({
  requests: [],
  
  handleWebSocketEvent: (event: WebSocketEvent) => {
    switch (event.type) {
      case 'request_received':
        // Create new request entry
        break;
      case 'scoring_complete':
        // Update request with scores
        break;
      case 'model_selected':
        // Update with provider/model selection
        break;
      case 'response_complete':
        // Finalize request with latency/cost
        break;
    }
  },
}));
```

## API Contracts

### NautRouter Required Endpoints

#### WebSocket Endpoint
```
WS /ws
```
Emits real-time routing events as JSON messages.

#### Profile Management
```
GET /v1/profile
Response: {
  "current": "auto",
  "available": ["eco", "auto", "premium"]
}

PUT /v1/profile
Body: { "profile": "premium" }
Response: { "success": true, "profile": "premium" }
```

#### Statistics Endpoint
```
GET /v1/stats?range=24h
Response: {
  "time_range": "24h",
  "total_requests": 1247,
  "total_cost": 12.34,
  "providers": [
    {
      "provider_id": "anthropic",
      "total_requests": 523,
      "total_cost": 8.21,
      "avg_latency": 1250,
      "success_rate": 0.99,
      "cost_trend": [...],
      "request_trend": [...]
    }
  ],
  "models": [
    {
      "model_id": "claude-opus-4",
      "provider_id": "anthropic", 
      "request_count": 234,
      "avg_cost": 0.045,
      "avg_latency": 1800
    }
  ],
  "savings": {
    "actual_cost": 12.34,
    "opus_baseline_cost": 45.67,
    "savings_usd": 33.33,
    "savings_percentage": 0.73
  },
  "profile_distribution": {
    "eco": 0.3,
    "auto": 0.6, 
    "premium": 0.1
  }
}
```

#### Provider Status
```
GET /v1/providers
Response: {
  "providers": [
    {
      "id": "anthropic",
      "name": "Anthropic",
      "status": "online",
      "models": [
        {
          "id": "claude-opus-4",
          "name": "Claude Opus 4",
          "cost_per_1k_tokens": 0.015,
          "status": "available"
        }
      ]
    }
  ]
}
```

## WebSocket Event Schema

### Event Flow Example
```json
// 1. Request received
{
  "type": "request_received",
  "request_id": "req_abc123",
  "timestamp": "2026-02-20T00:23:45.123Z",
  "data": {
    "agent_id": "jarvis",
    "message_preview": "How do I implement a React component...",
    "profile": "auto"
  }
}

// 2. Scoring complete
{
  "type": "scoring_complete", 
  "request_id": "req_abc123",
  "timestamp": "2026-02-20T00:23:45.234Z",
  "data": {
    "scores": {
      "code_keywords": 0.85,
      "reasoning_markers": 0.23,
      // ... all 14 dimensions
    },
    "complexity_tier": "medium"
  }
}

// 3. Model selected
{
  "type": "model_selected",
  "request_id": "req_abc123", 
  "timestamp": "2026-02-20T00:23:45.267Z",
  "data": {
    "provider": "anthropic",
    "model": "claude-sonnet-4",
    "reasoning": "Medium complexity coding task, Sonnet optimal cost/quality"
  }
}

// 4. Response complete
{
  "type": "response_complete",
  "request_id": "req_abc123",
  "timestamp": "2026-02-20T00:23:47.123Z", 
  "data": {
    "latency_ms": 1856,
    "cost_usd": 0.0234,
    "tokens_consumed": 1560,
    "success": true
  }
}
```

## Design System

### Color Palette
```javascript
// utils/colors.ts
export const colors = {
  providers: {
    anthropic: '#4F46E5',     // Indigo
    lmstudio: '#10B981',      // Emerald (local/free)
    gemini: '#F59E0B',        // Amber
  },
  complexity: {
    simple: '#10B981',        // Green
    medium: '#F59E0B',        // Amber  
    complex: '#EF4444',       // Red
  },
  status: {
    online: '#10B981',
    degraded: '#F59E0B', 
    offline: '#EF4444',
    processing: '#8B5CF6',
  }
};
```

### Theme Configuration
```javascript
// tailwind.config.js
module.exports = {
  content: ['./src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        'neon-blue': '#00D4FF',
        'neon-purple': '#8B5CF6',
        'neon-green': '#00FF88',
      },
      backdropBlur: {
        xs: '2px',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'flow': 'flow 2s ease-in-out infinite',
      }
    }
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@headlessui/tailwindcss'),
  ]
}
```

### Glassmorphism Components
```css
/* Base glass card style */
.glass-card {
  @apply bg-gray-900/30 backdrop-blur-md border border-gray-700/50 rounded-lg;
}

.glass-panel {
  @apply bg-gray-800/40 backdrop-blur-sm border-l border-gray-600/30;
}
```

## Implementation Notes

### Performance Considerations
- **Virtualized Lists:** Use `react-window` for request feed with 1000+ items
- **Debounced Updates:** Batch WebSocket events every 100ms to prevent UI thrashing
- **Memoization:** Wrap expensive chart calculations in `useMemo`
- **WebSocket Reconnection:** Exponential backoff, max 30s intervals

### State Management Strategy
- **Zustand stores** for different domains (requests, stats, config)
- **WebSocket events** drive state mutations
- **Local state** for UI-only concerns (modals, selected items)
- **Persist profile** selection to localStorage

### Error Handling
- WebSocket disconnection → show reconnection banner
- API errors → toast notifications with retry actions
- Failed routing requests → highlight in red, show error details
- Graceful degradation when stats API unavailable

### Accessibility
- Keyboard navigation for all interactive elements
- Screen reader labels for charts and visualizations  
- Color blind friendly palette with patterns/shapes
- ARIA live regions for real-time updates

## Development Phases

### Phase 1: Core Infrastructure (Week 1)
- [x] Project setup (Vite + React + TypeScript)
- [x] WebSocket connection management
- [x] Basic component structure
- [x] Zustand stores setup
- [x] Mock data for development

### Phase 2: Node Graph (Week 2)  
- [x] React Flow integration
- [x] Custom node components
- [x] Animated edges
- [x] Real-time request flow visualization
- [x] Provider/model status indicators

### Phase 3: Request Feed & Score Breakdown (Week 3)
- [x] Live request feed with virtualization
- [x] Score breakdown panel
- [x] Request selection and detail view
- [x] WebSocket event integration

### Phase 4: Analytics Dashboard (Week 4)
- [x] Stats API integration
- [x] Chart components (Recharts)
- [x] Time range selector
- [x] Cost savings calculations
- [x] Profile distribution metrics

### Phase 5: Profile Management & Polish (Week 5)
- [x] Profile selector component
- [x] NautRouter API integration
- [x] Error handling and loading states
- [x] Responsive design refinements
- [x] Performance optimization

## NautRouter Implementation Requirements

### WebSocket Server Addition

Add to NautRouter's `src/index.ts`:

```typescript
import { WebSocketServer } from 'ws';

const wss = new WebSocketServer({ port: 8403 });

// Broadcast to all connected clients
const broadcastEvent = (event: WebSocketEvent) => {
  wss.clients.forEach(client => {
    if (client.readyState === WebSocket.OPEN) {
      client.send(JSON.stringify(event));
    }
  });
};

// Integrate into request lifecycle
app.post('/v1/chat/completions', async (req, res) => {
  const requestId = generateId();
  
  // 1. Emit request received
  broadcastEvent({
    type: 'request_received',
    request_id: requestId,
    timestamp: new Date().toISOString(),
    data: {
      agent_id: req.headers['x-agent-id'],
      message_preview: req.body.messages[req.body.messages.length - 1].content.substring(0, 100),
      profile: currentProfile
    }
  });
  
  // 2. Run scoring engine
  const scores = scoreRequest(req.body);
  broadcastEvent({
    type: 'scoring_complete',
    request_id: requestId,
    timestamp: new Date().toISOString(), 
    data: { scores, complexity_tier: getComplexityTier(scores) }
  });
  
  // 3. Select model
  const { provider, model } = selectModel(scores, currentProfile);
  broadcastEvent({
    type: 'model_selected',
    request_id: requestId,
    timestamp: new Date().toISOString(),
    data: { provider, model, reasoning: getSelectionReasoning(scores, provider, model) }
  });
  
  // 4. Make request and emit completion
  const start = Date.now();
  try {
    const response = await makeProviderRequest(provider, model, req.body);
    const latency = Date.now() - start;
    
    broadcastEvent({
      type: 'response_complete',
      request_id: requestId,
      timestamp: new Date().toISOString(),
      data: {
        latency_ms: latency,
        cost_usd: calculateCost(response.usage, model),
        tokens_consumed: response.usage.total_tokens,
        success: true
      }
    });
    
    res.json(response);
  } catch (error) {
    broadcastEvent({
      type: 'error',
      request_id: requestId, 
      timestamp: new Date().toISOString(),
      data: { error: error.message }
    });
    res.status(500).json({ error: error.message });
  }
});
```

### Stats Endpoint Implementation

```typescript
app.get('/v1/stats', async (req, res) => {
  const range = req.query.range || '24h';
  const since = getTimeRangeStart(range);
  
  // Query memory API for historical data
  const response = await fetch(`http://100.71.163.122:8085/memories?category=cost&since=${since}`);
  const memories = await response.json();
  
  const stats = aggregateStats(memories, range);
  res.json(stats);
});
```

### Profile Management

```typescript
let currentProfile = 'auto';

app.get('/v1/profile', (req, res) => {
  res.json({
    current: currentProfile,
    available: ['eco', 'auto', 'premium']
  });
});

app.put('/v1/profile', (req, res) => {
  const { profile } = req.body;
  if (['eco', 'auto', 'premium'].includes(profile)) {
    currentProfile = profile;
    res.json({ success: true, profile });
  } else {
    res.status(400).json({ error: 'Invalid profile' });
  }
});
```

## Getting Started

### Prerequisites
- Node.js 18+
- npm or yarn
- NautRouter running on port 8402

### Installation

```bash
# Create new Vite project
npm create vite@latest naut-router-dashboard -- --template react-ts
cd naut-router-dashboard

# Install dependencies
npm install react-flow-renderer recharts @headlessui/react @heroicons/react
npm install zustand axios classnames date-fns
npm install -D tailwindcss @tailwindcss/forms

# Setup Tailwind
npx tailwindcss init -p
```

### Environment Variables

```bash
# .env
VITE_NAUTROUTER_HTTP_URL=http://localhost:8402
VITE_NAUTROUTER_WS_URL=ws://localhost:8403
VITE_MEMORY_API_URL=http://100.71.163.122:8085
```

### Development

```bash
npm run dev
```

Dashboard will be available at `http://localhost:5173`

---

**Total Estimated Development Time:** 4-5 weeks for full implementation
**Priority Order:** Node Graph → Request Feed → Stats → Profile Management → Polish
**Deployment Target:** Can be standalone or integrated into SpeedCoder dashboard as a tab

This specification provides everything needed to build a production-ready NautRouter visualization dashboard. The real-time WebSocket feed will create an engaging "mission control" experience for monitoring AI model routing decisions.