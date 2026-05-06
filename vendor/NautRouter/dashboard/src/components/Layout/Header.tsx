import { useConfigStore } from '../../stores/configStore';
import { getStatusColor } from '../../utils/colors';

export function Header() {
  const connectionStatus = useConfigStore((s) => s.connectionStatus);
  const currentProfile = useConfigStore((s) => s.currentProfile);

  return (
    <header className="glass-card flex items-center justify-between px-8 py-5 shrink-0">
      <div className="flex items-center gap-4">
        <div className="text-2xl font-bold text-white tracking-tight">
          NautRouter
        </div>
        <span className="text-[11px] text-neutral-500 font-mono mt-0.5">v2.0</span>
      </div>

      <div className="flex items-center gap-8">
        <div className="flex items-center gap-2.5 text-sm text-neutral-400">
          <span className="uppercase tracking-wider text-[11px]">Profile</span>
          <span className="font-mono text-white bg-neutral-800 px-2.5 py-1 rounded-md text-xs border border-neutral-700">
            {currentProfile}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <div
            className="w-2 h-2 rounded-full"
            style={{ backgroundColor: getStatusColor(connectionStatus === 'connected' ? 'online' : connectionStatus === 'connecting' ? 'degraded' : 'offline') }}
          />
          <span className="text-[11px] text-neutral-400 capitalize">{connectionStatus}</span>
        </div>
      </div>
    </header>
  );
}
