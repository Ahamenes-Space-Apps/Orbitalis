import { useEffect, useRef, useState } from 'react';
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Pause,
  Play,
  RotateCcw,
} from 'lucide-react';
import type { Frame } from '@/data/types';
import {
  formatMjdDate,
  formatMjdDay,
  formatMjdUtcDay,
  formatMjdUtcShortDay,
  formatMjdUtcTime,
  formatWavelength,
  formatWavelengthRange,
} from '@/lib/format';
import { inWavelengthRange, normalizeWavelengthRange, type WavelengthRange } from '@/lib/wavelength';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export type DockMode = 'epoch' | 'trails' | 'diff';

const MODES: Array<{ id: DockMode; label: string; disabled?: boolean; title?: string }> = [
  { id: 'epoch', label: 'Epoch' },
  { id: 'trails', label: 'Trails' },
  {
    id: 'diff',
    label: 'Temp Diff',
    disabled: true,
    title: 'Temperature-difference frames are not exposed by the API.',
  },
];

interface FrameScrubberProps {
  frames: Frame[];
  selectedRef: string | null;
  order: 'time' | 'wavelength';
  playing: boolean;
  speed: number;
  wavelengthRange?: WavelengthRange | null;
  collapsed: boolean;
  mode: DockMode;
  onCollapsed: (collapsed: boolean) => void;
  onMode: (mode: DockMode) => void;
  onOrder: (order: 'time' | 'wavelength') => void;
  onSelect: (frameRef: string) => void;
  onPlaying: (playing: boolean) => void;
  onSpeed: (speed: number) => void;
  onResetView: () => void;
}

export function FrameScrubber({
  frames,
  selectedRef,
  order,
  playing,
  speed,
  wavelengthRange: wavelengthRangeProp = null,
  collapsed,
  mode,
  onCollapsed,
  onMode,
  onOrder,
  onSelect,
  onPlaying,
  onSpeed,
  onResetView,
}: FrameScrubberProps) {
  const ordered = [...frames].sort((a, b) =>
    order === 'time' ? a.mjd - b.mjd : a.wavelength_um - b.wavelength_um,
  );
  const wavelengthRange = normalizeWavelengthRange(wavelengthRangeProp);
  const inSelectedRange = (frame: Frame) =>
    !wavelengthRange || inWavelengthRange(frame.wavelength_um, wavelengthRange);
  const index = ordered.findIndex((frame) => frame.frame_ref === selectedRef);
  const selected = index >= 0 ? ordered[index] : (ordered[0] ?? null);

  useEffect(() => {
    if (!playing || ordered.length < 2) return;
    const timer = window.setInterval(() => {
      const current = ordered.findIndex((frame) => frame.frame_ref === selectedRef);
      const next = (current + 1) % ordered.length;
      onSelect(ordered[next].frame_ref);
    }, Math.max(125, 1000 / speed));
    return () => window.clearInterval(timer);
  }, [onSelect, ordered, playing, selectedRef, speed]);

  const step = (direction: -1 | 1) => {
    if (ordered.length === 0) return;
    const current = index < 0 ? 0 : index;
    const next = (current + direction + ordered.length) % ordered.length;
    onSelect(ordered[next].frame_ref);
  };

  // Draggable timeline: press anywhere on the arc and drag (either
  // direction) to show the sky as it was at that point in the sequence.
  const trackRef = useRef<HTMLDivElement | null>(null);
  const scrubbing = useRef(false);

  const indexAt = (clientX: number) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || ordered.length === 0) return 0;
    const fraction = (clientX - rect.left) / rect.width;
    const clamped = Math.min(1, Math.max(0, fraction));
    return Math.min(
      ordered.length - 1,
      Math.round(clamped * (ordered.length - 1)),
    );
  };

  const scrubTo = (clientX: number) => {
    const next = indexAt(clientX);
    if (ordered[next] && ordered[next].frame_ref !== selectedRef) {
      onSelect(ordered[next].frame_ref);
    }
  };

  const knobFraction =
    ordered.length > 1 ? Math.max(0, index < 0 ? 0 : index) / (ordered.length - 1) : 0;

  // Curved timeline: a quadratic arc whose tick marks and knob sit on the
  // curve. Dragging maps the pointer's x position to the nearest frame.
  const [trackWidth, setTrackWidth] = useState(0);
  useEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    const update = () => setTrackWidth(el.getBoundingClientRect().width);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const arcWidth = Math.max(trackWidth, 240);
  const arcHeight = 88;
  const arcStart = { x: 14, y: 70 };
  const arcCtrl = { x: arcWidth / 2, y: -52 };
  const arcEnd = { x: arcWidth - 14, y: 70 };
  const arcPoint = (t: number) => {
    const u = 1 - t;
    return {
      x: u * u * arcStart.x + 2 * u * t * arcCtrl.x + t * t * arcEnd.x,
      y: u * u * arcStart.y + 2 * u * t * arcCtrl.y + t * t * arcEnd.y,
    };
  };
  // Arc-length lookup: bezier parameter steps land unevenly along the curve,
  // so ticks and the knob map through cumulative length instead.
  const arcTable = (() => {
    const steps = 120;
    const points: { x: number; y: number }[] = [];
    const cumulative: number[] = [0];
    for (let i = 0; i <= steps; i++) points.push(arcPoint(i / steps));
    for (let i = 1; i <= steps; i++) {
      cumulative.push(
        cumulative[i - 1] + Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y),
      );
    }
    return { points, cumulative, total: cumulative[steps] };
  })();
  const arcPointAtLength = (fraction: number) => {
    const target = Math.min(1, Math.max(0, fraction)) * arcTable.total;
    const { points, cumulative } = arcTable;
    let low = 0;
    let high = cumulative.length - 1;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (cumulative[mid] < target) low = mid + 1;
      else high = mid;
    }
    const i = Math.max(1, low);
    const span = cumulative[i] - cumulative[i - 1] || 1;
    const t = (target - cumulative[i - 1]) / span;
    return {
      x: points[i - 1].x + (points[i].x - points[i - 1].x) * t,
      y: points[i - 1].y + (points[i].y - points[i - 1].y) * t,
    };
  };
  const arcNormal = (t: number) => {
    const u = 1 - t;
    const tx = 2 * u * (arcCtrl.x - arcStart.x) + 2 * t * (arcEnd.x - arcCtrl.x);
    const ty = 2 * u * (arcCtrl.y - arcStart.y) + 2 * t * (arcEnd.y - arcCtrl.y);
    const len = Math.hypot(tx, ty) || 1;
    return { x: -ty / len, y: tx / len };
  };
  const arcPath = `M ${arcStart.x} ${arcStart.y} Q ${arcCtrl.x} ${arcCtrl.y} ${arcEnd.x} ${arcEnd.y}`;

  if (frames.length === 0) {
    return (
      <section
        aria-label="Frame scrubber"
        className="shrink-0 border-t border-line bg-panel px-3 py-2 text-[11px] text-haze"
        role="status"
      >
        Waiting for frames. The scrubber fills as observations arrive.
      </section>
    );
  }

  const mjdMin = Math.min(...frames.map((frame) => frame.mjd));
  const mjdMax = Math.max(...frames.map((frame) => frame.mjd));
  const waveMin = Math.min(...frames.map((frame) => frame.wavelength_um));
  const waveMax = Math.max(...frames.map((frame) => frame.wavelength_um));
  const inRangeCount = ordered.filter(inSelectedRange).length;

  return (
    <section
      aria-label="Frame scrubber"
      className="flex shrink-0 flex-col border-t border-line bg-panel"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 px-3 py-1.5">
        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            aria-label="Previous frame"
            onClick={() => step(-1)}
            className="size-9 rounded-full border border-line-2 text-mist hover:border-accent/70 hover:text-accent"
          >
            <ChevronLeft className="size-4" aria-hidden="true" />
          </Button>
          <Button
            variant="outline"
            size="icon"
            aria-label={playing ? 'Pause playback' : 'Play frames'}
            onClick={() => onPlaying(!playing)}
            className="size-11 rounded-full border-accent bg-accent text-accent-ink shadow-[0_0_18px_rgba(245,185,66,0.5)] hover:bg-accent-soft hover:text-accent-ink"
          >
            {playing ? (
              <Pause className="size-5" aria-hidden="true" />
            ) : (
              <Play className="ml-0.5 size-5" aria-hidden="true" />
            )}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label="Next frame"
            onClick={() => step(1)}
            className="size-9 rounded-full border border-line-2 text-mist hover:border-accent/70 hover:text-accent"
          >
            <ChevronRight className="size-4" aria-hidden="true" />
          </Button>
        </div>

        <div
          className="flex shrink-0 overflow-hidden rounded-sm border border-line"
          role="group"
          aria-label="Dock mode"
        >
          {MODES.map((item) => (
            <Button
              key={item.id}
              variant="segment"
              size="sm"
              className="min-h-7 px-2 text-[10px] whitespace-nowrap"
              data-active={mode === item.id}
              aria-pressed={mode === item.id}
              disabled={item.disabled}
              title={item.title}
              onClick={() => onMode(item.id)}
            >
              {item.label}
            </Button>
          ))}
        </div>

        <label className="flex shrink-0 items-center gap-1 text-[10px] text-haze">
          Order
          <select
            value={order}
            onChange={(event) => onOrder(event.target.value as 'time' | 'wavelength')}
            className="min-h-7 rounded-sm border border-line bg-canvas px-1.5 font-mono text-[10px] text-mist"
            aria-label="Order frames"
          >
            <option value="time">Time</option>
            <option value="wavelength">Wavelength</option>
          </select>
        </label>

        <label className="flex shrink-0 items-center gap-1 text-[10px] text-haze">
          Speed
          <select
            value={speed}
            onChange={(event) => onSpeed(Number(event.target.value))}
            className="min-h-7 rounded-full border border-line-2 bg-canvas px-2 font-mono text-[10px] text-mist"
            aria-label="Playback speed"
          >
            {[1, 2, 4, 8].map((value) => (
              <option key={value} value={value}>
                {value} fps
              </option>
            ))}
          </select>
        </label>

        <Button
          variant="outline"
          size="sm"
          className="min-h-7 shrink-0 px-2 text-[10px] whitespace-nowrap"
          aria-label="Reset sky view (All-Sky)"
          onClick={onResetView}
        >
          <RotateCcw className="size-3" aria-hidden="true" />
          All-Sky
        </Button>

        <div className="min-w-0 flex-1" />

        {selected && (
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[10px] text-mist">
            <span className="whitespace-nowrap" title={formatMjdDate(selected.mjd)}>
              {formatMjdDay(selected.mjd)}
            </span>
            <span className="text-line-2" aria-hidden="true">
              ·
            </span>
            <span className="whitespace-nowrap">{formatWavelength(selected.wavelength_um)}</span>
            <span className="text-line-2" aria-hidden="true">
              ·
            </span>
            <span className="whitespace-nowrap">D{selected.detector}</span>
            <span className="text-line-2" aria-hidden="true">
              ·
            </span>
            <span className="whitespace-nowrap">{selected.release.toUpperCase()}</span>
          </div>
        )}

        <Button
          variant="ghost"
          size="icon"
          className="size-7 min-h-0 shrink-0"
          aria-label={collapsed ? 'Expand frame dock' : 'Collapse frame dock'}
          aria-expanded={!collapsed}
          onClick={() => onCollapsed(!collapsed)}
        >
          {collapsed ? (
            <ChevronUp className="size-3.5" aria-hidden="true" />
          ) : (
            <ChevronDown className="size-3.5" aria-hidden="true" />
          )}
        </Button>
      </div>

      {!collapsed && (
        <div className="flex flex-col gap-1 px-3 pb-2">
          <div
            ref={trackRef}
            className="relative cursor-ew-resize touch-none select-none"
            role="slider"
            aria-label="Timeline scrubber"
            aria-valuemin={0}
            aria-valuemax={Math.max(0, ordered.length - 1)}
            aria-valuenow={index < 0 ? 0 : index}
            aria-valuetext={
              selected
                ? `${order === 'time' ? formatMjdDate(selected.mjd) : formatWavelength(selected.wavelength_um)} (frame ${(index < 0 ? 0 : index) + 1} of ${ordered.length})`
                : undefined
            }
            aria-orientation="horizontal"
            tabIndex={0}
            onPointerDown={(event) => {
              if (event.button !== 0) return;
              scrubbing.current = true;
              if (playing) onPlaying(false);
              scrubTo(event.clientX);
              try {
                event.currentTarget.setPointerCapture(event.pointerId);
              } catch {
                // Synthetic pointers (and a few edge browsers) have no active
                // pointer to capture; scrubbing still works without it.
              }
            }}
            onPointerMove={(event) => {
              if (scrubbing.current) scrubTo(event.clientX);
            }}
            onPointerUp={(event) => {
              scrubbing.current = false;
              try {
                event.currentTarget.releasePointerCapture(event.pointerId);
              } catch {
                // No capture was taken (see pointerdown).
              }
            }}
            onPointerCancel={() => {
              scrubbing.current = false;
            }}
            onKeyDown={(event) => {
              if (event.key === 'ArrowLeft') {
                event.preventDefault();
                step(-1);
              } else if (event.key === 'ArrowRight') {
                event.preventDefault();
                step(1);
              }
            }}
          >
            <svg
              width={arcWidth}
              height={arcHeight}
              viewBox={`0 0 ${arcWidth} ${arcHeight}`}
              className="block"
            >
              <path
                d={arcPath}
                fill="none"
                className="stroke-line"
                strokeWidth={1.5}
                strokeLinecap="round"
              />
              {ordered.map((frame, tickIndex) => {
                const t = ordered.length > 1 ? tickIndex / (ordered.length - 1) : 0;
                const point = arcPointAtLength(t);
                const normal = arcNormal(t);
                const active = frame.frame_ref === selected?.frame_ref;
                const dimmed = wavelengthRange !== null && !inSelectedRange(frame);
                const half = active ? 8 : 5;
                const showLabel =
                  ordered.length <= 90 ||
                  active ||
                  tickIndex === 0 ||
                  tickIndex === ordered.length - 1;
                return (
                  <g
                    key={frame.frame_ref}
                    role="button"
                    aria-label={`Show frame ${tickIndex + 1} of ${ordered.length}, ${formatWavelength(
                      frame.wavelength_um,
                    )}, ${formatMjdDate(frame.mjd)}${
                      dimmed ? ', outside selected wavelength range' : ''
                    }`}
                    aria-pressed={active}
                    className={cn('cursor-pointer', dimmed && 'opacity-35')}
                    onClick={() => onSelect(frame.frame_ref)}
                  >
                    <line
                      x1={point.x - normal.x * half}
                      y1={point.y - normal.y * half}
                      x2={point.x + normal.x * half}
                      y2={point.y + normal.y * half}
                      strokeWidth={active ? 2.5 : 1.5}
                      strokeLinecap="round"
                      className={active ? 'stroke-accent' : 'stroke-line-2 hover:stroke-mist'}
                    />
                    {showLabel && (
                      <text
                        x={point.x}
                        y={point.y + 20}
                        textAnchor="middle"
                        className={cn(
                          'fill-haze font-mono text-[9px] transition-colors',
                          active && 'fill-accent font-semibold',
                        )}
                      >
                        E{tickIndex + 1}
                      </text>
                    )}
                    <rect x={point.x - 7} y={point.y - 10} width={14} height={20} fill="transparent" />
                  </g>
                );
              })}
              {selected &&
                (() => {
                  const knob = arcPointAtLength(knobFraction);
                  return (
                    <g
                      style={{ transition: 'transform 75ms' }}
                      transform={`translate(${knob.x}, ${knob.y})`}
                    >
                      <circle r={10} className="fill-accent/25" />
                      <circle
                        r={5.5}
                        className="fill-accent"
                        style={{ filter: 'drop-shadow(0 0 8px rgba(245, 185, 66, 0.8))' }}
                      />
                    </g>
                  );
                })()}
            </svg>
          </div>
          <div className="relative flex items-start justify-between gap-3 font-mono text-[10px] text-haze">
            <span className="pt-0.5 uppercase">
              {order === 'time' ? formatMjdUtcShortDay(mjdMin) : formatWavelength(waveMin)}
            </span>
            {selected && (
              <div className="absolute left-1/2 -translate-x-1/2 text-center">
                <div className="text-[13px] font-semibold uppercase tracking-wider text-mist">
                  {order === 'time'
                    ? formatMjdUtcDay(selected.mjd)
                    : formatWavelength(selected.wavelength_um)}
                </div>
                <div className="text-[10px] uppercase tracking-widest text-haze">
                  {order === 'time'
                    ? formatMjdUtcTime(selected.mjd)
                    : `frame ${(index < 0 ? 0 : index) + 1} of ${ordered.length}`}
                </div>
                <div className="text-[10px] text-haze">
                  {wavelengthRange
                    ? `${inRangeCount} of ${ordered.length} in ${formatWavelengthRange(wavelengthRange)}`
                    : `${ordered.length} epochs stored`}
                </div>
              </div>
            )}
            <span className="pt-0.5 text-right uppercase">
              {order === 'time' ? formatMjdUtcShortDay(mjdMax) : formatWavelength(waveMax)}
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
