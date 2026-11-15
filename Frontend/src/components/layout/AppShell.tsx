import { useEffect, type ReactNode } from 'react';
import { NavLink, useOutlet } from 'react-router-dom';
import { Moon, Sun } from 'lucide-react';
import { formatDec, formatGalactic, formatRa } from '@/lib/coords';
import { useCapabilities, useField } from '@/data/hooks';
import { usingMockApi } from '@/data/api';
import { useSearchPreservingPath, useUrlState } from '@/state/urlState';
import { useUi } from '@/store/useUi';
import { ModeBanner } from '@/components/common/ModeBanner';
import { FieldEntry } from '@/components/explorer/FieldEntry';
import { AssistantLauncher } from '@/components/assistant/AssistantLauncher';
import { AssistantPanel } from '@/components/assistant/AssistantPanel';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const navClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    'relative flex h-full shrink-0 items-center whitespace-nowrap border-b-2 px-2.5 font-mono text-[10px] tracking-[0.16em] uppercase no-underline transition-colors sm:px-3',
    isActive
      ? 'border-accent text-ink'
      : 'border-transparent text-haze hover:text-ink',
  );

function NavItem({ to, end, children }: { to: string; end?: boolean; children: ReactNode }) {
  const preserve = useSearchPreservingPath();
  return (
    <NavLink to={preserve(to)} end={end} className={navClass}>
      {children}
    </NavLink>
  );
}

export function AppShell() {
  const { values, update } = useUrlState();
  const { data: fieldEnvelope } = useField(values.f);
  const { data: capabilities } = useCapabilities();
  // Mock mode has no chat transport, so a dead control is never shown even if
  // the bundled capability snapshot says the assistant is on.
  const assistantEnabled = !usingMockApi && Boolean(capabilities?.data.assistant.enabled);
  const frameSystem = values.sys;
  const cursor = useUi((state) => state.cursor);
  const fieldEntryOpen = useUi((state) => state.fieldEntryOpen);
  const setFieldEntryOpen = useUi((state) => state.setFieldEntryOpen);
  const theme = useUi((state) => state.theme);
  const toggleTheme = useUi((state) => state.toggleTheme);
  const outlet = useOutlet();
  const field = fieldEnvelope?.data;

  const readout = cursor
    ? frameSystem === 'eq'
      ? `RA ${formatRa(cursor.ra)} · Dec ${formatDec(cursor.dec)}`
      : formatGalactic(cursor)
    : field
      ? frameSystem === 'eq'
        ? `RA ${formatRa(field.grid.ra)} · Dec ${formatDec(field.grid.dec)}`
        : formatGalactic({ ra: field.grid.ra, dec: field.grid.dec })
      : null;

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    document.documentElement.style.colorScheme = theme;
  }, [theme]);

  return (
    <div className="cosmic-canvas flex h-screen flex-col overflow-hidden text-ink">
      <header className="relative flex h-[56px] shrink-0 items-center gap-3 border-b border-line bg-canvas/60 px-3 backdrop-blur-xl sm:px-4">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/80 to-transparent"
        />

        {/* Zone 1 — wordmark */}
        <NavLink
          to="/"
          className="flex shrink-0 items-center gap-2 text-ink no-underline"
          aria-label="ORBITALIS home"
        >
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
          <span className="hidden text-[13px] font-bold tracking-[0.22em] sm:inline">ORBITALIS</span>
        </NavLink>

        <span aria-hidden="true" className="h-5 w-px shrink-0 bg-line" />

        {/* Zone 2 — primary tabs */}
        <nav
          className="flex h-full min-w-0 items-stretch overflow-x-auto"
          aria-label="Main"
        >
          <NavItem to="/explorer" end>
            <span className="sm:hidden">Sky</span>
            <span className="hidden sm:inline">Sky Explorer</span>
          </NavItem>
          <NavItem to="/candidates">
            <span className="sm:hidden">Dossier</span>
            <span className="hidden sm:inline">Candidate Dossier</span>
          </NavItem>
        </nav>

        <div className="min-w-0 flex-1" />

        {/* Zone 3 — context and theme */}
        {field && (
          <button
            type="button"
            onClick={() => setFieldEntryOpen(true)}
            className="hidden max-w-[260px] cursor-pointer items-center gap-2 border border-line bg-panel/60 px-2 py-1 text-left text-[11px] text-mist backdrop-blur transition-colors hover:border-line-2 hover:text-ink lg:flex"
            data-testid="field-chip"
            aria-label="Current field. Open field entry."
          >
            <span className="truncate font-mono">
              {formatRa(field.grid.ra)} · {formatDec(field.grid.dec)} · r {field.params.radius_deg}°
              · {field.params.release.toUpperCase()}
            </span>
            <span className="shrink-0 text-[10px] text-haze">Change</span>
          </button>
        )}

        {readout && (
          <div
            className="hidden shrink-0 font-mono text-[10px] text-haze xl:block"
            data-testid="coord-readout"
          >
            {readout}
          </div>
        )}

        <div
          className="hidden shrink-0 overflow-hidden rounded-md border border-line sm:flex"
          role="group"
          aria-label="Coordinate frame"
        >
          <Button
            variant="segment"
            size="sm"
            className="min-h-7 px-2 text-[11px]"
            data-active={frameSystem === 'eq'}
            aria-pressed={frameSystem === 'eq'}
            onClick={() => update({ sys: 'eq' })}
          >
            Equatorial
          </Button>
          <Button
            variant="segment"
            size="sm"
            className="min-h-7 px-2 text-[11px]"
            data-active={frameSystem === 'gal'}
            aria-pressed={frameSystem === 'gal'}
            onClick={() => update({ sys: 'gal' })}
          >
            Galactic
          </Button>
        </div>

        <button
          type="button"
          onClick={toggleTheme}
          aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          className="grid size-8 shrink-0 cursor-pointer place-items-center border border-line bg-panel/60 text-mist backdrop-blur transition-colors hover:border-line-2 hover:text-ink"
        >
          {theme === 'dark' ? (
            <Sun className="size-3.5" aria-hidden="true" />
          ) : (
            <Moon className="size-3.5" aria-hidden="true" />
          )}
        </button>
      </header>

      <ModeBanner />

      <div className="flex min-h-0 flex-1">
        <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto bg-transparent">
          <div className="flex min-h-0 flex-1 flex-col">{outlet}</div>
        </main>
      </div>

      {assistantEnabled && <AssistantPanel />}

      {assistantEnabled && <AssistantLauncher />}

      {fieldEntryOpen && <FieldEntry variant="dialog" />}
    </div>
  );
}
