import { useRequestStore } from '../../stores/requestStore';
import { RequestItem } from './RequestItem';

export function RequestFeed() {
  const requests = useRequestStore((s) => s.requests);
  const selectedRequest = useRequestStore((s) => s.selectedRequest);
  const selectRequest = useRequestStore((s) => s.selectRequest);

  return (
    <div className="glass-card flex flex-col h-full bg-neutral-900/50 border-neutral-800">
      <div className="px-3 py-2 border-b border-neutral-800 flex items-center justify-between">
        <h3 className="text-xs uppercase tracking-wider text-neutral-500">Request Feed</h3>
        <span className="text-[10px] text-neutral-600 font-mono">{requests.length}</span>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-1">
        {requests.length > 0 ? (
          requests.map((request) => (
            <RequestItem
              key={request.id}
              request={request}
              isSelected={selectedRequest?.id === request.id}
              onClick={() => selectRequest(request)}
            />
          ))
        ) : (
          <div className="flex items-center justify-center h-full text-sm text-neutral-600 p-8">
            No requests yet. Send a request to NautRouter to see it here.
          </div>
        )}
      </div>
    </div>
  );
}
