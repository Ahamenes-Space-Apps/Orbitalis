import { Clock3, Database, ScanSearch, X } from 'lucide-react';
import type { AssistantContextItem } from '@/store/useAssistant';

const icons = {
  field: Database,
  candidate: ScanSearch,
  frame: Clock3,
} as const;

export function ContextChips({
  items,
  onRemove,
}: {
  items: AssistantContextItem[];
  onRemove: (item: AssistantContextItem) => void;
}) {
  if (items.length === 0) return null;

  return (
    <div className="flex items-center gap-1.5" aria-label="Assistant context">
      {items.map((item) => {
        const Icon = icons[item.kind];
        return (
          <span
            key={`${item.kind}:${item.id}`}
            className="inline-flex max-w-[220px] shrink-0 items-center gap-1 rounded-[4px] border border-line bg-panel px-1.5 py-0.5 text-[10.5px] text-mist"
          >
            <Icon className="size-3 shrink-0 text-haze" aria-hidden="true" />

            <span className="max-w-[160px] truncate">{item.label}</span>

            <button
              type="button"
              onClick={() => onRemove(item)}
              aria-label={`Remove ${item.label} from context`}
              className="grid size-3.5 shrink-0 cursor-pointer place-items-center rounded-[2px] text-haze transition-colors hover:bg-panel-2 hover:text-ink"
            >
              <X className="size-2.5" aria-hidden="true" />
            </button>
          </span>
        );
      })}
    </div>
  );
}
