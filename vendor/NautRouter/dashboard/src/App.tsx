import { Layout } from './components/Layout/Layout';
import { NodeGraph } from './components/NodeGraph/NodeGraph';
import { RequestFeed } from './components/RequestFeed/RequestFeed';
import { ScoreBreakdown } from './components/ScoreBreakdown/ScoreBreakdown';
import { StatsDashboard } from './components/StatsDashboard/StatsDashboard';
import { ProfileSelector } from './components/ProfileSelector/ProfileSelector';
import { useWebSocket } from './hooks/useWebSocket';
import { useNautRouter } from './hooks/useNautRouter';
import { useMockData } from './hooks/useMockData';

const MOCK_MODE = typeof window !== 'undefined' && window.location.search.includes('mock');

function App() {
  useWebSocket();
  useNautRouter();
  useMockData(MOCK_MODE);

  return (
    <Layout>
      <div className="flex flex-1 gap-5 h-full min-h-0">
        <div className="flex-1 flex flex-col gap-5 min-w-0 overflow-y-auto">
          <NodeGraph />
          <ProfileSelector />
          <StatsDashboard />
        </div>
        <div className="w-80 flex flex-col gap-5 shrink-0 min-h-0">
          <div className="flex-1 min-h-0">
            <RequestFeed />
          </div>
          <ScoreBreakdown />
        </div>
      </div>
    </Layout>
  );
}

export default App
