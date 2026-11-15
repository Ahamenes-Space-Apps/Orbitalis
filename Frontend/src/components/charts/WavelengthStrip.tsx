import { useMemo, useRef } from 'react';
import type { Frame, Spectrum } from '@/data/types';
import { formatWavelength, formatWavelengthRange } from '@/lib/format';
import { cn } from '@/lib/utils';
import {
  SPECTRAL_BAND_COUNT,
  SPECTRAL_BAND_MAX_UM,
  SPECTRAL_BAND_MIN_UM,
  bandIndexForWavelength,
  bandWavelengthForIndex,
  inWavelengthRange,
  normalizeWavelengthRange,
  spectralColorForWavelength,
  wavelengthKey,
  type WavelengthRange,
} from '@/lib/wavelength';

interface WavelengthStripProps {
  frames: Array<Pick<Frame, 'wavelength_um' | 'detector'>>;
  spectrum?: Spectrum | null;
  selectedWavelength?: number | null;
  selection?: WavelengthRange | null;
  onSelectionChange?: (range: WavelengthRange | null) => void;
  className?: string;
}

interface BandSample {
  frames: number;
  detectors: number[];
  spectrumSamples: number;
}

/**
 * The 102 SPHEREx bands as a slim vertical spectrum rail: 0.75 µm at the top,
 * 5.0 µm at the bottom, painted with the positional rainbow hue of each band.
 * The band matching the current frame's wavelength is highlighted. Drag
 * vertically to select a band range, or focus it and use the up/down arrow
 * keys (Shift+arrows change the width, Escape clears). The hue is a wavelength
 * cue, not a calibrated colour.
 */
export function WavelengthStrip({
  frames,
  spectrum,
  selectedWavelength = null,
  selection = null,
  onSelectionChange,
  className,
}: WavelengthStripProps) {
  const ref = useRef<HTMLDivElement>(null);
  const anchor = useRef<number | null>(null);
  const moved = useRef(false);

  const bandSamples = useMemo(() => {
    const samples = new Map<number, BandSample>();
    const ensure = (index: number) => {
      let sample = samples.get(index);
      if (!sample) {
        sample = { frames: 0, detectors: [], spectrumSamples: 0 };
        samples.set(index, sample);
      }
      return sample;
    };
    for (const frame of frames) {
      if (!Number.isFinite(frame.wavelength_um)) continue;
      const sample = ensure(bandIndexForWavelength(frame.wavelength_um));
      sample.frames += 1;
      if (!sample.detectors.includes(frame.detector)) sample.detectors.push(frame.detector);
    }
    for (const point of spectrum?.points ?? []) {
      if (!Number.isFinite(point.wavelength_um)) continue;
      ensure(bandIndexForWavelength(point.wavelength_um)).spectrumSamples += 1;
    }
    return samples;
  }, [frames, spectrum]);

  const activeRange = normalizeWavelengthRange(selection);
  const count = SPECTRAL_BAND_COUNT;

  const activeIndex =
    selectedWavelength !== null && Number.isFinite(selectedWavelength)
      ? bandIndexForWavelength(selectedWavelength)
      : null;

  const indexAt = (clientY: number) => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect || rect.height === 0) return 0;
    const f = Math.min(1, Math.max(0, (clientY - rect.top) / rect.height));
    return Math.min(count - 1, Math.floor(f * count));
  };

  const rangeForIndices = (a: number, b: number): WavelengthRange => {
    const low = Math.min(a, b);
    const high = Math.max(a, b);
    return [
      wavelengthKey(bandWavelengthForIndex(low)),
      wavelengthKey(bandWavelengthForIndex(high)),
    ];
  };

  const selectedIndices = activeRange
    ? Array.from({ length: count }, (_, index) => index).filter((index) =>
        inWavelengthRange(bandWavelengthForIndex(index), activeRange),
      )
    : [];
  const firstSelected = selectedIndices.length ? selectedIndices[0] : null;
  const lastSelected = selectedIndices.length ? selectedIndices[selectedIndices.length - 1] : null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!onSelectionChange) return;
    if (
      e.key !== 'ArrowLeft' &&
      e.key !== 'ArrowRight' &&
      e.key !== 'ArrowUp' &&
      e.key !== 'ArrowDown'
    ) {
      if (e.key === 'Escape') {
        e.preventDefault();
        onSelectionChange(null);
      }
      return;
    }
    e.preventDefault();
    const d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1;

    if (firstSelected === null) {
      const index = d === 1 ? 0 : count - 1;
      onSelectionChange(rangeForIndices(index, index));
      return;
    }

    if (e.shiftKey) {
      if (d === 1) {
        const nextEdge = Math.min(count - 1, (lastSelected ?? firstSelected) + 1);
        onSelectionChange(rangeForIndices(firstSelected, nextEdge));
      } else {
        const nextEdge = Math.max(0, firstSelected - 1);
        onSelectionChange(rangeForIndices(nextEdge, lastSelected ?? firstSelected));
      }
      return;
    }

    const width = lastSelected !== null ? lastSelected - firstSelected : 0;
    const latestStart = count - 1 - width;
    const nextStart = Math.min(latestStart, Math.max(0, firstSelected + d));
    onSelectionChange(rangeForIndices(nextStart, nextStart + width));
  };

  const activeWavelength = activeIndex !== null ? bandWavelengthForIndex(activeIndex) : null;
  const readout = activeRange
    ? formatWavelengthRange(activeRange)
    : activeWavelength !== null
      ? formatWavelength(activeWavelength)
      : '—';

  return (
    <div className={cn('flex w-fit flex-col items-center gap-1.5', className)}>
      <div className="text-[8px] font-semibold tracking-[0.12em] text-haze uppercase">Wavelength</div>
      <span className="font-mono text-[9px] text-haze">{formatWavelength(SPECTRAL_BAND_MIN_UM)}</span>
      <div
        ref={ref}
        tabIndex={onSelectionChange ? 0 : undefined}
        role={onSelectionChange ? 'group' : undefined}
        aria-label="Vertical wavelength bands from 0.75 to 5.0 µm. The current wavelength is highlighted. Drag vertically to select, or use the arrow keys."
        className={cn(
          'relative flex h-[305px] w-8 cursor-ns-resize touch-none flex-col gap-px rounded-sm transition-shadow duration-300',
          activeRange && 'ring-1 ring-violet/50 ring-offset-2 ring-offset-bar',
          !onSelectionChange && 'cursor-default',
        )}
        onPointerDown={(e) => {
          if (!onSelectionChange || e.button !== 0) return;
          anchor.current = indexAt(e.clientY);
          moved.current = false;
          e.currentTarget.setPointerCapture?.(e.pointerId);
        }}
        onPointerMove={(e) => {
          if (!onSelectionChange || anchor.current === null) return;
          const i = indexAt(e.clientY);
          if (i !== anchor.current) moved.current = true;
          if (moved.current) onSelectionChange(rangeForIndices(anchor.current, i));
        }}
        onPointerUp={() => {
          if (!onSelectionChange) return;
          if (!moved.current) onSelectionChange(null);
          anchor.current = null;
        }}
        onPointerCancel={() => {
          anchor.current = null;
          moved.current = false;
        }}
        onKeyDown={onKeyDown}
        data-testid="wavelength-strip"
      >
        {Array.from({ length: count }, (_, index) => {
          const wavelength = bandWavelengthForIndex(index);
          const sample = bandSamples.get(index);
          const inRange = !activeRange || inWavelengthRange(wavelength, activeRange);
          const active = index === activeIndex;
          const color = spectralColorForWavelength(wavelength);
          const stored = sample?.frames ?? 0;
          const coverageLabel = stored
            ? `${stored} frame${stored === 1 ? '' : 's'}${
                sample && sample.detectors.length
                  ? ` · detector ${sample.detectors.map((id) => `D${id}`).join(', ')}`
                  : ''
              }`
            : sample && sample.spectrumSamples > 0
              ? `${sample.spectrumSamples} SED sample${sample.spectrumSamples === 1 ? '' : 's'}`
              : 'no stored frame';
          return (
            <div
              key={index}
              aria-hidden="true"
              data-band-index={index}
              data-active={active ? 'true' : undefined}
              title={`B${String(index + 1).padStart(3, '0')} · ${formatWavelength(wavelength)} · ${coverageLabel}${active ? ' · current wavelength' : ''}`}
              className={cn(
                'min-h-0 flex-1 rounded-[1px] transition-[opacity,transform,box-shadow] duration-300',
                active ? 'z-10 scale-x-125' : 'hover:scale-x-110',
              )}
              style={{
                background: color,
                opacity: active ? 1 : inRange ? 0.95 : 0.2,
                boxShadow: active
                  ? `0 0 0 1px var(--color-ink), 0 0 8px 2px ${color}`
                  : undefined,
              }}
            />
          );
        })}
      </div>
      <span className="font-mono text-[9px] text-haze">{formatWavelength(SPECTRAL_BAND_MAX_UM)}</span>
      <div
        className="font-mono text-[10px] whitespace-nowrap text-ink"
        data-testid="wavelength-strip-label"
      >
        {readout}
      </div>
      {activeRange && (
        <span className="font-mono text-[9px] text-haze">
          {selectedIndices.length} of {count}
        </span>
      )}
    </div>
  );
}
