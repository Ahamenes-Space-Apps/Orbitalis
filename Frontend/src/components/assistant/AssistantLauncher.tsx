import { Sparkles } from 'lucide-react';
import { useAssistant } from '@/store/useAssistant';

export function AssistantLauncher() {
  const open = useAssistant((state) => state.open);
  const setOpen = useAssistant((state) => state.setOpen);
  const hasMessages = useAssistant((state) => state.messages.length > 0);

  if (open) return null;

  return (
    <button
      type="button"
      onClick={() => setOpen(true)}
      aria-label="Open the assistant"
      className="group fixed right-5 bottom-5 z-30 inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-full border border-accent bg-accent px-4 py-2.5 text-[13px] font-semibold text-accent-ink shadow-[0_18px_50px_rgba(66,96,230,0.35)] transition-colors hover:bg-ink hover:text-canvas max-sm:size-12 max-sm:justify-center max-sm:p-0"
    >
      <Sparkles className="size-4 shrink-0" aria-hidden="true" />
      <span className="max-sm:hidden">Ask the assistant</span>
      {hasMessages && (
        <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-current opacity-70" />
      )}
    </button>
  );
}
