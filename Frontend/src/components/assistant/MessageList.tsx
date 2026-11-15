import { useEffect, useRef, useState } from 'react';
import { Check, Copy, ScanSearch, Sparkles } from 'lucide-react';
import type { AssistantMessage } from '@/store/useAssistant';
import { cn } from '@/lib/utils';

const INLINE_PATTERN = /(#\d{3}|\*\*[^*]+\*\*|`[^`]+`)/g;

function InlineText({
  content,
  onCandidate,
}: {
  content: string;
  onCandidate: (ordinal: number) => void;
}) {
  return (
    <>
      {content.split(INLINE_PATTERN).map((part, index) => {
        if (/^#\d{3}$/.test(part)) {
          return (
            <button
              key={index}
              type="button"
              onClick={() => onCandidate(Number(part.slice(1)))}
              className="cursor-pointer font-mono text-accent underline decoration-accent/40 underline-offset-2 transition-colors hover:text-ink"
            >
              {part}
            </button>
          );
        }

        if (/^\*\*[^*]+\*\*$/.test(part)) {
          return (
            <strong key={index} className="font-semibold text-ink">
              {part.slice(2, -2)}
            </strong>
          );
        }

        if (/^`[^`]+`$/.test(part)) {
          return (
            <code
              key={index}
              className="mx-0.5 rounded-[2px] border border-line bg-panel-2 px-1 py-px font-mono text-[0.9em] text-ink"
            >
              {part.slice(1, -1)}
            </code>
          );
        }

        return <span key={index}>{part}</span>;
      })}
    </>
  );
}

type Block =
  | { type: 'paragraph'; text: string }
  | { type: 'list'; ordered: boolean; items: string[] };

function blocksOf(content: string): Block[] {
  return content
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block): Block => {
      const lines = block
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean);

      if (lines.length > 0 && lines.every((line) => /^[-*•]\s+/.test(line))) {
        return {
          type: 'list',
          ordered: false,
          items: lines.map((line) => line.replace(/^[-*•]\s+/, '')),
        };
      }

      if (lines.length > 0 && lines.every((line) => /^\d+[.)]\s+/.test(line))) {
        return {
          type: 'list',
          ordered: true,
          items: lines.map((line) => line.replace(/^\d+[.)]\s+/, '')),
        };
      }

      return { type: 'paragraph', text: block };
    });
}

function MessageBody({
  content,
  onCandidate,
}: {
  content: string;
  onCandidate: (ordinal: number) => void;
}) {
  const blocks = blocksOf(content);

  return (
    <div className="flex flex-col gap-2 text-[13px] leading-relaxed text-mist">
      {blocks.map((block, blockIndex) => {
        if (block.type === 'list') {
          const items = block.items.map((item, itemIndex) => (
            <li key={itemIndex} className="pl-0.5">
              <InlineText content={item} onCandidate={onCandidate} />
            </li>
          ));
          const listClass = cn(
            'm-0 flex list-outside flex-col gap-1 pl-4 marker:text-accent',
            block.ordered ? 'list-decimal' : 'list-disc',
          );

          return block.ordered ? (
            <ol key={blockIndex} className={listClass}>
              {items}
            </ol>
          ) : (
            <ul key={blockIndex} className={listClass}>
              {items}
            </ul>
          );
        }

        return (
          <p key={blockIndex} className="m-0 whitespace-pre-line">
            <InlineText content={block.text} onCandidate={onCandidate} />
          </p>
        );
      })}
    </div>
  );
}

function ToolCalls({ message }: { message: AssistantMessage }) {
  if (message.toolCalls.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1">
      {message.toolCalls.map((call) => (
        <span
          key={call.name}
          className="inline-flex items-center gap-1 rounded-[3px] border border-line bg-panel-2 px-1.5 py-0.5 font-mono text-[9px] tracking-[0.02em] text-haze"
        >
          <ScanSearch className="size-2.5" aria-hidden="true" />
          {call.label}
        </span>
      ))}
    </div>
  );
}

function TypingDots() {
  return (
    <span className="flex items-center gap-1 py-1" aria-label="The assistant is thinking">
      {[0, 1, 2].map((index) => (
        <span
          key={index}
          className="size-1.5 animate-pulse rounded-full bg-accent"
          style={{ animationDelay: `${index * 120}ms` }}
        />
      ))}
    </span>
  );
}

export function MessageList({
  messages,
  streamingId,
  onCandidate,
}: {
  messages: AssistantMessage[];
  streamingId: string | null;
  onCandidate: (ordinal: number) => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const last = messages[messages.length - 1];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, last?.content, last?.status]);

  async function copy(message: AssistantMessage) {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopiedId(message.id);
      window.setTimeout(() => {
        setCopiedId((current) => (current === message.id ? null : current));
      }, 1600);
    } catch {
      setCopiedId(null);
    }
  }

  if (messages.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2.5 px-6 py-6 text-center">
        <span className="grid size-11 place-items-center rounded-[8px] border border-line-2 bg-panel text-accent">
          <Sparkles className="size-5" aria-hidden="true" />
        </span>
        <div>
          <p className="m-0 text-[15px] font-semibold text-ink">How can I help?</p>
          <p className="m-0 mt-1 max-w-[30ch] text-[12px] leading-relaxed text-haze">
            Ask about this field, a candidate, or how the search works.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-3.5 py-4">
      {messages.map((message) => {
        if (message.role === 'user') {
          return (
            <div key={message.id} className="flex justify-end">
              <div className="max-w-[85%] rounded-[14px] rounded-br-[4px] bg-accent px-3.5 py-2.5 text-[13px] leading-relaxed whitespace-pre-wrap text-accent-ink shadow-[0_6px_20px_rgba(66,96,230,0.28)]">
                {message.content}
              </div>
            </div>
          );
        }

        const thinking =
          message.status === 'streaming' &&
          message.content.length === 0 &&
          message.toolCalls.length === 0 &&
          message.id === streamingId;
        const copied = copiedId === message.id;

        return (
          <div key={message.id} className="flex items-start gap-2.5">
            <span
              aria-hidden="true"
              className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-[6px] border border-line-2 bg-panel text-accent"
            >
              <Sparkles className="size-3.5" />
            </span>

            <div className="flex min-w-0 flex-1 flex-col gap-1.5">
              <div
                className={cn(
                  'flex flex-col gap-2 rounded-[14px] rounded-tl-[4px] border px-3.5 py-3',
                  message.status === 'error'
                    ? 'border-danger/50 bg-danger/5'
                    : 'border-line bg-panel/70',
                )}
              >
                <ToolCalls message={message} />

                {thinking ? (
                  <TypingDots />
                ) : (
                  <div className="flex items-start gap-1.5">
                    <div className="min-w-0 flex-1">
                      <MessageBody content={message.content} onCandidate={onCandidate} />
                    </div>
                    {message.status === 'streaming' && message.content.length > 0 && (
                      <span
                        aria-hidden="true"
                        className="mt-1.5 h-3 w-[2px] shrink-0 animate-pulse bg-accent"
                      />
                    )}
                  </div>
                )}

                {message.status === 'stopped' && (
                  <span className="font-mono text-[9px] tracking-[0.08em] text-haze uppercase">
                    Stopped
                  </span>
                )}

                {message.status === 'error' && (
                  <span className="text-[11px] leading-snug text-danger" role="alert">
                    {message.error?.message ??
                      'The assistant is unavailable right now. The rest of the app keeps working.'}
                  </span>
                )}
              </div>

              {message.content.length > 0 && !thinking && (
                <div className="flex justify-end">
                  <button
                    type="button"
                    onClick={() => void copy(message)}
                    className="inline-flex cursor-pointer items-center gap-1 px-1 py-0.5 font-mono text-[9px] tracking-[0.04em] text-haze uppercase transition-colors hover:text-ink"
                    aria-label="Copy answer"
                  >
                    {copied ? (
                      <Check className="size-2.5" aria-hidden="true" />
                    ) : (
                      <Copy className="size-2.5" aria-hidden="true" />
                    )}
                    {copied ? 'Copied' : 'Copy'}
                  </button>
                </div>
              )}
            </div>
          </div>
        );
      })}

      <div ref={bottomRef} />
    </div>
  );
}
