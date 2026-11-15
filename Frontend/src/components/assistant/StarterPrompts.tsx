import { useState } from 'react';
import { ArrowUpRight, ChevronDown } from 'lucide-react';

export interface StarterPrompt {
  label: string;
  prompt: string;
}

/**
 * The five starter questions from the backend knowledge pack
 * (`config/knowledge/starter_questions.md`). They cover only documented
 * capabilities, and every answer still carries the unconfirmed footnote.
 */
export const STARTER_PROMPTS: StarterPrompt[] = [
  {
    label: 'What does this candidate’s stack SNR mean?',
    prompt: "What does this candidate's stack SNR mean?",
  },
  {
    label: 'How should I read the model score next to this candidate?',
    prompt: 'How should I read the model score next to this candidate?',
  },
  {
    label: 'How do I know how well the search actually works?',
    prompt: 'How do I know how well the search actually works?',
  },
  {
    label: 'Which pixels are masked, and what do the flag bits mean?',
    prompt: 'Which pixels are masked, and what do the flag bits mean?',
  },
  {
    label: 'What is the difference between observed, predicted and candidate positions?',
    prompt: 'What is the difference between observed, predicted and candidate positions?',
  },
];

export function StarterPrompts({
  prompts,
  onPick,
  disabled,
}: {
  prompts: StarterPrompt[];
  onPick: (prompt: string) => void;
  disabled?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? prompts : prompts.slice(0, 3);

  return (
    <div className="flex flex-col gap-1.5" aria-label="Starter questions">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[9px] tracking-[0.16em] text-haze uppercase">
          Try asking
        </span>

        {prompts.length > 3 && (
          <button
            type="button"
            onClick={() => setExpanded((current) => !current)}
            className="inline-flex cursor-pointer items-center gap-0.5 font-mono text-[9px] tracking-[0.08em] text-haze uppercase transition-colors hover:text-ink"
          >
            {expanded ? 'Show less' : `Show all ${prompts.length}`}
            <ChevronDown
              className={`size-2.5 transition-transform ${expanded ? 'rotate-180' : ''}`}
              aria-hidden="true"
            />
          </button>
        )}
      </div>

      <div className="flex max-h-[196px] flex-col gap-1 overflow-y-auto pr-0.5">
        {visible.map((item) => (
          <button
            key={item.prompt}
            type="button"
            onClick={() => onPick(item.prompt)}
            disabled={disabled}
            className="group flex cursor-pointer items-start gap-2 rounded-[6px] border border-line bg-panel px-2.5 py-1.5 text-left text-[11.5px] leading-snug text-mist transition-colors hover:border-accent/60 hover:bg-panel-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
          >
            <ArrowUpRight
              className="mt-px size-3 shrink-0 text-haze transition-colors group-hover:text-accent"
              aria-hidden="true"
            />
            <span className="line-clamp-2">{item.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
