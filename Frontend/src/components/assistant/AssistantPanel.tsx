import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Sparkles, SquarePen, X } from 'lucide-react';
import { DURATION, EASE } from '@/lib/motion';
import {
  formatDec,
  formatId,
  formatMjdDate,
  formatRa,
  formatWavelength,
} from '@/lib/format';
import { useCandidates, useField, useFrames } from '@/data/hooks';
import type { ChatContext } from '@/data/types';
import { useUrlState } from '@/state/urlState';
import {
  useAssistant,
  type AssistantContextItem,
} from '@/store/useAssistant';
import { ContextChips } from './ContextChips';
import { MessageList } from './MessageList';
import { STARTER_PROMPTS, StarterPrompts } from './StarterPrompts';
import { AssistantComposer } from './AssistantComposer';

const contextKey = (item: AssistantContextItem) => `${item.kind}:${item.id}`;

export function AssistantPanel() {
  const open = useAssistant((state) => state.open);
  const setOpen = useAssistant((state) => state.setOpen);
  const messages = useAssistant((state) => state.messages);
  const streamingId = useAssistant((state) => state.streamingId);
  const send = useAssistant((state) => state.send);
  const stop = useAssistant((state) => state.stop);
  const clear = useAssistant((state) => state.clear);

  const { values, update } = useUrlState();
  const field = useField(values.f);
  const frames = useFrames(values.f);
  const candidates = useCandidates(values.f);

  const [removed, setRemoved] = useState<string[]>([]);

  const candidateList = candidates.data?.data.candidates ?? [];
  const selectedCandidate =
    candidateList.find((candidate) => candidate.candidate_id === values.sel) ?? null;
  const selectedFrame =
    (frames.data?.data.frames ?? []).find((frame) => frame.frame_ref === values.t) ?? null;

  const context = useMemo<AssistantContextItem[]>(() => {
    const items: AssistantContextItem[] = [];
    const fieldData = field.data?.data;

    if (fieldData) {
      const presetLabel = fieldData.params.preset ? 'Preset field' : 'Field';
      items.push({
        kind: 'field',
        id: fieldData.field_id,
        label: `${presetLabel} · ${formatRa(fieldData.params.ra)} · ${formatDec(fieldData.params.dec)}`,
      });
    }

    if (selectedCandidate) {
      items.push({
        kind: 'candidate',
        id: selectedCandidate.candidate_id,
        label: `Candidate ${formatId(selectedCandidate.ordinal)}`,
      });
    }

    if (selectedFrame) {
      items.push({
        kind: 'frame',
        id: selectedFrame.frame_ref,
        label: `Frame ${formatMjdDate(selectedFrame.mjd)} · ${formatWavelength(
          selectedFrame.wavelength_um,
        )}`,
      });
    }

    return items;
  }, [field.data, selectedCandidate, selectedFrame]);

  const activeContext = context.filter((item) => !removed.includes(contextKey(item)));

  const chatContext = useMemo<ChatContext>(() => {
    const focused = (kind: AssistantContextItem['kind']) =>
      activeContext.find((item) => item.kind === kind)?.id ?? null;
    return {
      field_id: focused('field'),
      frame_ref: focused('frame'),
      candidate_id: focused('candidate'),
    };
  }, [activeContext]);

  const streaming = streamingId !== null;

  function handleSend(prompt: string) {
    void send(prompt, chatContext, activeContext);
  }

  function handleCandidate(ordinal: number) {
    const match = candidateList.find((candidate) => candidate.ordinal === ordinal);
    if (match) update({ sel: match.candidate_id });
  }

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.aside
          key="assistant-panel"
          aria-label="Assistant"
          initial={{ opacity: 0, y: 20, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 16, scale: 0.98 }}
          transition={{ duration: DURATION.base, ease: EASE }}
          className="fixed right-4 bottom-4 z-30 flex h-[min(620px,calc(100dvh-2rem))] w-[min(400px,calc(100vw-2rem))] flex-col overflow-hidden rounded-[10px] border border-line-2 bg-bar/95 shadow-[0_24px_80px_rgba(0,0,0,0.6)] backdrop-blur-2xl max-sm:inset-x-3 max-sm:bottom-3 max-sm:top-16 max-sm:h-auto max-sm:w-auto"
        >
          <header className="flex shrink-0 items-center gap-2.5 border-b border-line px-3.5 py-3">
            <span className="relative grid size-9 shrink-0 place-items-center rounded-[6px] border border-line-2 bg-accent-soft text-accent">
              <Sparkles className="size-4" aria-hidden="true" />
              <span className="absolute -right-1 -bottom-1 size-2.5 rounded-full bg-ok ring-2 ring-canvas" />
            </span>

            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <h2 className="m-0 truncate text-[14px] font-semibold text-ink">
                  Orbitals Assistant
                </h2>
                <span className="shrink-0 border border-line px-1 py-px font-mono text-[8px] tracking-[0.12em] text-haze uppercase">
                  AI
                </span>
              </div>
              <p className="m-0 mt-0.5 truncate text-[10.5px] text-haze">
                Grounded in this field&apos;s stored results
              </p>
            </div>

            <div className="flex shrink-0 items-center gap-1">
              {messages.length > 0 && (
                <button
                  type="button"
                  onClick={clear}
                  aria-label="Start a new chat"
                  title="New chat"
                  className="grid size-7 cursor-pointer place-items-center rounded-[4px] text-haze transition-colors hover:bg-panel-2 hover:text-ink"
                >
                  <SquarePen className="size-3.5" aria-hidden="true" />
                </button>
              )}

              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close assistant"
                className="grid size-7 cursor-pointer place-items-center rounded-[4px] text-haze transition-colors hover:bg-panel-2 hover:text-ink"
              >
                <X className="size-4" aria-hidden="true" />
              </button>
            </div>
          </header>

          {activeContext.length > 0 && (
            <div className="flex shrink-0 items-center gap-2 border-b border-line px-3.5 py-2">
              <span className="shrink-0 font-mono text-[9px] tracking-[0.16em] text-haze uppercase">
                Using
              </span>
              <div className="min-w-0 flex-1 overflow-x-auto">
                <ContextChips
                  items={activeContext}
                  onRemove={(item) => setRemoved((current) => [...current, contextKey(item)])}
                />
              </div>
            </div>
          )}

          <MessageList
            messages={messages}
            streamingId={streamingId}
            onCandidate={handleCandidate}
          />

          {messages.length === 0 && (
            <div className="shrink-0 px-3.5 pb-2">
              <StarterPrompts prompts={STARTER_PROMPTS} onPick={handleSend} disabled={streaming} />
            </div>
          )}

          <div className="shrink-0 border-t border-line p-3">
            <AssistantComposer onSend={handleSend} onStop={stop} streaming={streaming} />

            <p className="m-0 mt-2 text-center text-[10px] leading-normal text-haze">
              AI answer from stored results · Candidates are unconfirmed
            </p>
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
