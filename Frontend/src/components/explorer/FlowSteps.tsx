import { cn } from '@/lib/utils';

export type ExplorerStage = 1 | 2 | 3;

const STEPS = [
  {
    title: 'Find a field',
    detail: 'Choose a patch to inspect.',
    body: 'Start with a preset below. Each one opens a real SPHEREx field with stored observations to explore.',
    action: 'Choose a field below',
  },
  {
    title: 'Watch the frames',
    detail: 'Compare the sky over time.',
    body: 'Once a field opens, play the epoch roller and watch the same patch change from frame to frame.',
    action: 'Use the epoch roller',
  },
  {
    title: 'Review the evidence',
    detail: 'Open one candidate dossier.',
    body: 'Select a candidate to focus its stored track, then review the light curve and spectrum. Every result stays unconfirmed.',
    action: 'Open a candidate',
  },
];

export function FlowSteps({
  active,
  className,
}: {
  active: ExplorerStage;
  className?: string;
}) {
  return (
    <section className={cn('flex flex-col gap-4', className)} aria-label="How to explore a field">
      <div>
        <p className="m-0 text-[12px] font-semibold uppercase tracking-[0.08em] text-accent">
          A short route through the sky
        </p>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="m-0 max-w-[58ch] text-[14px] leading-relaxed text-mist">
            Pick a field, watch its stored frames, then open one candidate when the motion catches your eye.
          </p>
          <span className="text-[11px] text-haze sm:hidden">Swipe for next →</span>
        </div>
      </div>
      <ol className="flow-deck m-0 flex list-none gap-3 overflow-x-auto snap-x snap-mandatory p-0 sm:grid sm:grid-cols-3 sm:overflow-visible">
          {STEPS.map((step, index) => {
            const number = index + 1;
            const current = number === active;
            const complete = number < active;
            return (
              <li
                key={step.title}
                className={cn(
                  'flex min-h-[220px] min-w-[82vw] snap-start flex-col justify-between border p-5 sm:min-w-0',
                  current
                    ? 'border-accent bg-accent-soft'
                    : 'border-line bg-panel',
                )}
              >
                <div className="flex items-start justify-between gap-4">
                <div
                  className={cn(
                    'grid size-9 shrink-0 place-items-center border font-mono text-[14px]',
                    current || complete
                      ? 'border-accent bg-accent text-accent-ink'
                      : 'border-line-2 text-haze',
                  )}
                  aria-hidden="true"
                >
                  {number}
                </div>
                {current && (
                  <span className="text-[11px] font-semibold text-accent-ink">Start here</span>
                )}
                </div>
                <div className="mt-8">
                  <p className="m-0 text-[20px] font-semibold tracking-[-0.02em] text-ink">{step.title}</p>
                  <p className="m-0 mt-1 text-[13px] font-medium text-mist">{step.detail}</p>
                  <p className="m-0 mt-3 text-[13px] leading-relaxed text-mist">{step.body}</p>
                </div>
                <p className={cn('m-0 mt-6 border-t pt-3 text-[12px] font-semibold', current ? 'border-accent/40 text-accent-ink' : 'border-line text-haze')}>
                  {step.action}
                </p>
              </li>
            );
          })}
      </ol>
    </section>
  );
}
