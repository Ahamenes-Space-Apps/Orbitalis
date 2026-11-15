import { Link } from 'react-router-dom';
import { ArrowRight, Moon, Sun } from 'lucide-react';
import { useUi } from '@/store/useUi';
import { Button } from '@/components/ui/button';
import { FlowSteps } from '@/components/explorer/FlowSteps';

function SignalGraphic() {
  const stars = [
    [28, 32, 1.4],
    [68, 72, 1],
    [104, 42, 1.2],
    [146, 92, 1.5],
    [184, 46, 1],
    [226, 108, 1.25],
    [276, 58, 1.6],
    [320, 132, 1],
    [342, 38, 1.1],
    [74, 144, 0.9],
  ];

  return (
    <figure className="onboarding-signal m-0" aria-label="A scan line crossing a stored sky field">
      <div className="onboarding-signal-frame">
        <svg viewBox="0 0 360 190" role="img" aria-hidden="true">
          <rect width="360" height="190" fill="none" />
          <path className="onboarding-signal-grid" d="M0 38H360M0 95H360M0 152H360M72 0V190M180 0V190M288 0V190" />
          {stars.map(([cx, cy, r], index) => (
            <circle key={index} className="onboarding-signal-star" cx={cx} cy={cy} r={r} />
          ))}
          <path className="onboarding-signal-track" d="M76 145C136 126 198 98 278 50" />
          <circle className="onboarding-signal-mover" cx="76" cy="145" r="4" />
          <circle className="onboarding-signal-target" cx="278" cy="50" r="8" />
          <line className="onboarding-signal-scan" x1="24" y1="0" x2="24" y2="190" />
        </svg>
        <div className="onboarding-signal-readout" aria-hidden="true">
          <span>STORED FRAMES</span>
          <span>LOOK FOR THE SHIFT</span>
        </div>
      </div>
      <figcaption>One faint mover, separated from a sky that holds still.</figcaption>
    </figure>
  );
}

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
        <section className="grid items-end gap-10 lg:grid-cols-[minmax(0,0.9fr)_minmax(360px,0.8fr)]">
          <div className="max-w-[760px]">
            <p className="app-kicker m-0">Before you enter the sky</p>
            <h1 className="m-0 mt-4 max-w-[15ch] text-4xl font-bold leading-[0.98] tracking-[-0.055em] sm:text-7xl">
              See what moves in real sky data.
            </h1>
            <p className="m-0 mt-5 max-w-[62ch] text-[16px] leading-relaxed text-mist">
              ORBITALIS is a time-aware sky explorer for repeated SPHEREx observations. Choose a field,
              compare its frames, and inspect faint movers without losing the evidence behind them.
            </p>
          </div>
          <SignalGraphic />
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
          <Button asChild size="default" variant="outline" className="onboarding-cta">
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
