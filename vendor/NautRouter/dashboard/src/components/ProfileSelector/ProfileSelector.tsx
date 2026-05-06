import { useConfigStore } from '../../stores/configStore';
import { PROFILE_DESCRIPTIONS } from '../../utils/constants';

export function ProfileSelector() {
  const currentProfile = useConfigStore((s) => s.currentProfile);
  const changeProfile = useConfigStore((s) => s.changeProfile);

  return (
    <div className="glass-card px-5 py-3 flex items-center gap-4 shrink-0">
      <h3 className="text-[10px] uppercase tracking-widest text-neutral-600 shrink-0">Profile</h3>
      <div className="flex gap-2">
        {Object.entries(PROFILE_DESCRIPTIONS).map(([id, info]) => (
          <button
            key={id}
            onClick={() => changeProfile(id)}
            className={`px-3 py-1.5 rounded-lg text-left transition-all border ${
              currentProfile === id
                ? 'border-neutral-600 bg-neutral-800'
                : 'border-transparent bg-neutral-900/30 hover:bg-neutral-800/60 hover:border-neutral-700'
            }`}
          >
            <span className={`text-xs font-medium ${currentProfile === id ? 'text-white' : 'text-neutral-400'}`}>
              {info.name}
            </span>
            <span className="text-[9px] text-neutral-600 ml-2 hidden lg:inline">{info.description}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
