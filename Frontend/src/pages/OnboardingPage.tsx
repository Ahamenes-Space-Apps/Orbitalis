import { Link } from 'react-router-dom';
import { ArrowRight, Moon, Sun } from 'lucide-react';
import { useUi } from '@/store/useUi';
import { Button } from '@/components/ui/button';
import { FlowSteps } from '@/components/explorer/FlowSteps';

export function OnboardingPage() {
  const theme = useUi((state) => state.theme);
  const toggleTheme = useUi((state) => state.toggleTheme);

  return (
    <main className="onboarding-page min-h-full overflow-y-auto bg-canvas text-ink">
      <header className="flex h-16 items-center justify-between border-b border-line px-4 sm:px-8">
        <Link to="/" className="flex items-center gap-2 text-ink no-underline" aria-label="ORBITALIS home">
          <svg width="28" height="28" viewBox="0 0 30 30" fill="none" aria-hidden="true">
            <ellipse
              cx="15"
              cy="15"
              rx="13"
              ry="5.5"
              transform="rotate(-30 15 15)"
              stroke="var(--accent)"
              strokeWidth="2"
            />
            <ellipse
              cx="15"
              cy="15"
              rx="13"
              ry="5.5"
              transform="rotate(30 15 15)"
              stroke="currentColor"
              strokeWidth="1.2"
            />
            <circle cx="15" cy="15" r="2.6" fill="currentColor" />
          </svg>
          <span className="text-[13px] font-bold tracking-[0.22em]">ORBITALIS</span>
        </Link>
        <button
          type="button"
          onClick={toggleTheme}
          aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          className="grid size-10 place-items-center border border-line bg-panel text-mist transition-colors hover:border-line-2 hover:text-ink"
        >
          {theme === 'dark' ? <Sun className="size-4" aria-hidden="true" /> : <Moon className="size-4" aria-hidden="true" />}
        </button>
      </header>

      <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-8 px-4 py-10 sm:px-8 sm:py-16">
        <section className="max-w-[760px]">
          <p className="app-kicker m-0">Before you enter the sky</p>
          <h1 className="m-0 mt-4 max-w-[15ch] text-4xl font-bold leading-[0.98] tracking-[-0.055em] sm:text-7xl">
            See what moves in real sky data.
          </h1>
          <p className="m-0 mt-5 max-w-[62ch] text-[16px] leading-relaxed text-mist">
            ORBITALIS is a time-aware sky explorer for repeated SPHEREx observations. Choose a field,
            compare its frames, and inspect faint movers without losing the evidence behind them.
          </p>
        </section>

        <FlowSteps active={1} />

        <section className="flex flex-wrap items-center justify-between gap-5 border-t border-line pt-6" aria-labelledby="onboarding-action-heading">
          <div>
            <h2 id="onboarding-action-heading" className="m-0 text-[17px] font-semibold">
              Ready to look at a field?
            </h2>
            <p className="m-0 mt-1 text-[13px] text-haze">
              Start with a preset or bring your own coordinates in the Explorer.
            </p>
          </div>
          <Button asChild size="default">
            <Link to="/explorer">
              Enter Sky Explorer
              <ArrowRight className="size-4" aria-hidden="true" />
            </Link>
          </Button>
        </section>
      </div>
    </main>
  );
}
