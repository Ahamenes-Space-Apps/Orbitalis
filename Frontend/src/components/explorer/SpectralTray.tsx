import { useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import type { Frame } from '@/data/types';
import { formatWavelength } from '@/lib/format';
import { cn } from '@/lib/utils';

const BAND_MIN = 0.75;
const BAND_MAX = 5.0;
const BAND_COUNT = 102;

const DIAGNOSTICS = [
  { um: 1.0, label: '1.0' },
  { um: 1.9, label: '1.9' },
  { um: 2.7, label: '2.7' },
  { um: 3.0, label: '3.0' },
  { um: 3.4, label: '3.4' },
  { um: 4.26, label: '4.26' },
];

const bandWavelength = (index: number) =>
  BAND_MIN + (index / (BAND_COUNT - 1)) * (BAND_MAX - BAND_MIN);

const bandForWavelength = (wavelength: number) => {
  const fraction = (wavelength - BAND_MIN) / (BAND_MAX - BAND_MIN);
  return Math.max(0, Math.min(BAND_COUNT - 1, Math.round(fraction * (BAND_COUNT - 1))));
};

interface SpectralTrayProps {
  frames: Frame[];
  selectedRef: string | null;
  collapsed: boolean;
  onToggle: () => void;
  onSelectWavelength?: (wavelength: number) => void;
  className?: string;
}

/**
 * The 102-band view strip. It is a wavelength ruler over the stored frames,
 * not a calibrated spectrum: bands without a stored frame are drawn as empty
 * slots and the diagnostic markers are reference wavelengths only.
 */
export function SpectralTray({
  frames,
  selectedRef,
  collapsed,
  onToggle,
  onSelectWavelength,
  className,
}: SpectralTrayProps) {
  const [hover, setHover] = useState<number | null>(null);

  const frameBands = new Map<number, Frame[]>();
  for (const frame of frames) {
    const band = bandForWavelength(frame.wavelength_um);
    frameBands.set(band, [...(frameBands.get(band) ?? []), frame]);
  }
  const selected = frames.find((frame) => frame.frame_ref === selectedRef) ?? frames[0] ?? null;
  const hoveredWavelength = hover === null ? null : bandWavelength(hover);

  return (
    <section
      className={cn('shrink-0 border-t border-line bg-panel', className)}
      aria-label="Spectral band tray"
    >
      <div className="flex h-8 items-center gap-3 px-3">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-label={collapsed ? 'Expand spectral tray' : 'Collapse spectral tray'}
          className="flex cursor-pointer items-center gap-1 text-haze transition-colors hover:text-ink"
        >
          {collapsed ? (
            <ChevronUp className="size-3.5" aria-hidden="true" />
          ) : (
            <ChevronDown className="size-3.5" aria-hidden="true" />
          )}
          <span className="text-[10px] font-semibold tracking-[0.14em] uppercase">Spectral tray</span>
        </button>
        <span className="font-mono text-[10px] text-haze">
          B001–B102 · 0.75–5.00 μm
        </span>
        <span className="ml-auto font-mono text-[10px] text-mist">
          {hoveredWavelength !== null
            ? `${formatWavelength(hoveredWavelength)} · B${String((hover ?? 0) + 1).padStart(3, '0')}`
            : selected
              ? `${formatWavelength(selected.wavelength_um)} · active`
              : 'Awaiting frames'}
        </span>
      </div>

      {!collapsed && (
        <div className="px-3 pb-2">
          <div className="relative">
            <div
              className="flex h-7 gap-px"
              role="group"
              aria-label="Spectral bands"
              onMouseLeave={() => setHover(null)}
            >
              {Array.from({ length: BAND_COUNT }, (_, index) => {
                const stored = frameBands.get(index);
                const active = selected ? bandForWavelength(selected.wavelength_um) === index : false;
                const hovered = hover === index;
                return (
                  <button
                    key={index}
                    type="button"
                    title={`B${String(index + 1).padStart(3, '0')} · ${formatWavelength(
                      bandWavelength(index),
                    )}${stored ? ` · ${stored.length} frame${stored.length === 1 ? '' : 's'}` : ' · no stored frame'}`}
                    aria-label={`Band ${index + 1}, ${formatWavelength(bandWavelength(index))}${
                      stored ? `, ${stored.length} stored frame${stored.length === 1 ? '' : 's'}` : ', empty'
                    }`}
                    onMouseEnter={() => setHover(index)}
                    onFocus={() => setHover(index)}
                    onBlur={() => setHover(null)}
                    onClick={() => onSelectWavelength?.(bandWavelength(index))}
                    className={cn(
                      'min-w-0 flex-1 cursor-pointer border-0 p-0 transition-colors',
                      active
                        ? 'bg-accent'
                        : stored
                          ? 'bg-accent/45'
                          : hovered
                            ? 'bg-accent/25'
                            : 'bg-panel-2',
                    )}
                  />
                );
              })}
            </div>

            {hover !== null && (
              <div
                aria-hidden="true"
                className="pointer-events-none absolute inset-y-0 w-px bg-ink/70"
                style={{ left: `${(hover / (BAND_COUNT - 1)) * 100}%` }}
              />
            )}

            {DIAGNOSTICS.map((line) => {
              const left = ((line.um - BAND_MIN) / (BAND_MAX - BAND_MIN)) * 100;
              return (
                <div
                  key={line.um}
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-y-0"
                  style={{ left: `${left}%` }}
                >
                  <div className="h-full border-l border-dashed border-accent/45" />
                  <span className="absolute -top-0.5 left-1 font-mono text-[8px] text-haze">
                    {line.label}
                  </span>
                </div>
              );
            })}
          </div>

          <div className="mt-1 flex items-center justify-between font-mono text-[9px] text-haze">
            <span>B001 · 0.75 μm</span>
            <span className="hidden sm:inline">diagnostic lines are reference wavelengths</span>
            <span>B102 · 5.00 μm</span>
          </div>
        </div>
      )}
    </section>
  );
}
