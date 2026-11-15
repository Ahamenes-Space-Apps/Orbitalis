import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { CornerDownLeft, SendHorizontal, Square } from 'lucide-react';

export function AssistantComposer({
  onSend,
  onStop,
  streaming,
}: {
  onSend: (prompt: string) => void;
  onStop: () => void;
  streaming: boolean;
}) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const trimmed = value.trim();

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = '0px';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 128)}px`;
  }, [value]);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!trimmed || streaming) return;
    onSend(trimmed);
    setValue('');
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <label className="sr-only" htmlFor="assistant-input">
        Ask the assistant
      </label>

      <div className="flex items-end gap-1.5 rounded-[10px] border border-line-2 bg-panel/80 p-1.5 transition-colors focus-within:border-accent">
        <textarea
          ref={textareaRef}
          id="assistant-input"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          maxLength={4000}
          enterKeyHint="send"
          autoComplete="off"
          placeholder="Ask about this field or selection…"
          className="max-h-32 min-h-[34px] w-full flex-1 resize-none bg-transparent px-1.5 py-2 text-[13px] leading-relaxed text-ink outline-none placeholder:text-haze"
        />

        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="Stop generating"
            title="Stop"
            className="grid size-8 shrink-0 cursor-pointer place-items-center rounded-full border border-line-2 text-mist transition-colors hover:border-danger hover:text-danger"
          >
            <Square className="size-3" aria-hidden="true" />
          </button>
        ) : (
          <button
            type="submit"
            disabled={!trimmed}
            aria-label="Send message"
            className="grid size-8 shrink-0 cursor-pointer place-items-center rounded-full bg-accent text-accent-ink transition-colors hover:bg-ink hover:text-canvas disabled:cursor-not-allowed disabled:opacity-40"
          >
            <SendHorizontal className="size-4" aria-hidden="true" />
          </button>
        )}
      </div>

      <span className="flex items-center gap-1 px-1 font-mono text-[9px] tracking-[0.04em] text-haze uppercase">
        <CornerDownLeft className="size-2.5" aria-hidden="true" />
        Enter to send · Shift + Enter for a new line
      </span>
    </form>
  );
}
