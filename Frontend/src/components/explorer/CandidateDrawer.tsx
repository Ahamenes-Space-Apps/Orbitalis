import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Check, Copy, Crosshair, FileText, Link2, MousePointer2, Sparkles, X } from 'lucide-react';
import { api } from '@/data/api';
import {
  useCandidate,
  useCandidateLightcurve,
  useCandidateSpectrum,
  useCapabilities,
} from '@/data/hooks';
import type { Field } from '@/data/types';
import {
  formatArcsec,
  formatDec,
  formatId,
  formatMjdDate,
  formatPositionAngle,
  formatRa,
  formatRate,
  formatRelease,
  formatScore,
  formatSnr,
  formatWavelength,
} from '@/lib/format';
import { inWavelengthRange, type WavelengthRange } from '@/lib/wavelength';
import { ErrorNote } from '@/components/common/ErrorNote';
import { Skeleton } from '@/components/common/Primitives';
import { LightcurveChart } from '@/components/charts/LightcurveChart';
import { SpectrumView } from '@/components/charts/SpectrumView';
import { SpectralExplorer } from '@/components/charts/SpectralExplorer';
import { WavelengthCoverage } from '@/components/charts/WavelengthCoverage';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

function rateAndAngle(detail: {
  motion: { mu_e_arcsec_per_day: number; mu_n_arcsec_per_day: number };
}) {
  const rate = Math.hypot(detail.motion.mu_e_arcsec_per_day, detail.motion.mu_n_arcsec_per_day);
  const angle =
    (Math.atan2(detail.motion.mu_e_arcsec_per_day, detail.motion.mu_n_arcsec_per_day) * 180) /
    Math.PI;
  return { rate, angle };
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function Cutout({
  src,
  alt,
  className,
  crosshair = false,
}: {
  src: string;
  alt: string;
  className?: string;
  crosshair?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div
        className={cn(
          'grid place-items-center border border-dashed border-line-2 bg-canvas p-4 text-center text-[11px] leading-snug text-haze',
          className,
        )}
      >
        This frame is not loaded on the server.
      </div>
    );
  }
  return (
    <div className={cn('relative overflow-hidden border border-line bg-[#0b1220]', className)}>
      <img
        src={src}
        alt={alt}
        loading="lazy"
        className="block h-full w-full object-contain"
        onError={() => setFailed(true)}
      />
      {crosshair && (
        <svg
          aria-hidden="true"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          className="pointer-events-none absolute inset-0 h-full w-full"
        >
          <line x1="50" x2="50" y1="42" y2="58" stroke="#39b8c4" strokeWidth="0.8" />
          <line x1="42" x2="58" y1="50" y2="50" stroke="#39b8c4" strokeWidth="0.8" />
          <rect x="43" y="43" width="14" height="14" fill="none" stroke="#39b8c4" strokeOpacity="0.55" strokeWidth="0.6" />
        </svg>
      )}
    </div>
  );
}

function SectionTitle({ id, children, aside }: { id: string; children: string; aside?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <h3 id={id} className="m-0 text-[12px] font-semibold tracking-[0.02em]">
        {children}
      </h3>
      {aside && <span className="font-mono text-[10px] text-haze">{aside}</span>}
    </div>
  );
}

export interface CandidateDrawerProps {
  candidateId: string;
  field: Field;
  variant?: 'drawer' | 'pane';
  wavelengthRange?: WavelengthRange | null;
  onWavelengthRange?: (range: WavelengthRange | null) => void;
  onClose?: () => void;
  onSelectOnMap?: () => void;
  /** The dossier page supplies its own header and export controls. */
  hideHeader?: boolean;
  /** The dossier page shows coverage as a read-only record. */
  readOnlyWavelength?: boolean;
  className?: string;
}

/**
 * Candidate detail (UX §6.2). The stack and the original cutouts sit side by
 * side, because seeing whether the source moves in the originals is the
 * evidence. The candidate is unconfirmed everywhere, including the copy block.
 */
export function CandidateDrawer({
  candidateId,
  field,
  variant = 'drawer',
  wavelengthRange: wavelengthRangeProp,
  onWavelengthRange,
  onClose,
  onSelectOnMap,
  hideHeader = false,
  readOnlyWavelength = false,
  className,
}: CandidateDrawerProps) {
  const detail = useCandidate(candidateId);
  const lightcurve = useCandidateLightcurve(candidateId);
  const spectrum = useCandidateSpectrum(candidateId);
  const capabilities = useCapabilities();
  const rankingEnabled = Boolean(capabilities.data?.data.ranking.enabled);
  const classificationEnabled = Boolean(capabilities.data?.data.classification.enabled);
  const [frameIndex, setFrameIndex] = useState(0);
  const [blinking, setBlinking] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [spectrumMode, setSpectrumMode] = useState<'102band' | 'sed'>('102band');
  const [localWavelengthRange, setLocalWavelengthRange] = useState<WavelengthRange | null>(null);
  const wavelengthRange = onWavelengthRange ? (wavelengthRangeProp ?? null) : localWavelengthRange;
  const setWavelengthRange = onWavelengthRange ?? setLocalWavelengthRange;

  const candidate = detail.data?.data;
  const frames = useMemo(
    () => [...(candidate?.frames ?? [])].sort((a, b) => a.mjd - b.mjd),
    [candidate?.frames],
  );

  useEffect(() => {
    setFrameIndex(0);
    setBlinking(false);
    setLocalWavelengthRange(null);
  }, [candidateId]);

  useEffect(() => {
    if (!onClose) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    if (!blinking || frames.length < 2) return;
    let last = false;
    const timer = window.setInterval(() => {
      last = !last;
      setFrameIndex(last ? frames.length - 1 : 0);
    }, 700);
    return () => window.clearInterval(timer);
  }, [blinking, frames.length]);

  const current = frames[Math.min(frameIndex, Math.max(0, frames.length - 1))];

  const metrics = useMemo(() => {
    if (!candidate) return null;
    const baseline =
      frames.length > 1
        ? frames[frames.length - 1].mjd - frames[0].mjd
        : Math.max(0, (frames[0]?.mjd ?? candidate.reference_mjd) - candidate.reference_mjd);
    const { rate, angle } = rateAndAngle(candidate);
    const shiftArcsec = rate * baseline;
    const shiftPx = shiftArcsec / Math.max(field.grid.pixel_scale_arcsec, 1e-6);
    return { baseline, rate, angle, shiftArcsec, shiftPx };
  }, [candidate, field.grid.pixel_scale_arcsec, frames]);

  const provenance = useMemo(() => {
    if (!candidate) return '';
    const lines = [
      `Candidate ${formatId(candidate.ordinal)} (${candidate.candidate_id})`,
      'Status: unconfirmed',
      `Position: RA ${formatRa(candidate.position.ra)} · Dec ${formatDec(candidate.position.dec)}`,
      `Motion: ${formatRate(metrics?.rate ?? 0)} at ${formatPositionAngle(metrics?.angle ?? 0)}`,
      `Stack SNR: ${formatSnr(candidate.stack_snr)}`,
      `Job: ${candidate.job_id}`,
      `Field: ${field.field_id}`,
      `Release: ${formatRelease(field.params.release)}`,
      `Reference MJD: ${candidate.reference_mjd}`,
      '',
      'Frames:',
      ...frames.map(
        (frame) =>
          `  ${formatMjdDate(frame.mjd)} · ${formatWavelength(frame.wavelength_um)} · D${frame.detector} · ${frame.frame_ref}`,
      ),
      '',
      'Candidates are unconfirmed. Independent follow-up is required for confirmation.',
    ];
    return lines.join('\n');
  }, [candidate, field.field_id, field.params.release, frames, metrics]);

  const copy = async (label: string, text: string) => {
    if (await copyText(text)) {
      setCopied(label);
      window.setTimeout(() => setCopied(null), 1600);
    }
  };

  const triageValue =
    rankingEnabled && candidate?.model_score
      ? formatScore(candidate.model_score.value)
      : candidate
        ? formatSnr(candidate.stack_snr)
        : '—';
  const triageBasis = rankingEnabled && candidate?.model_score ? 'model score' : 'stack SNR';

  const body = (
    <div className="flex h-full min-h-0 flex-col">
      {!hideHeader && (
        <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
          <div className="flex min-w-0 flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="m-0 font-mono text-lg font-semibold text-ink">
                {formatId(candidate?.ordinal)}
              </h2>
              <Badge tone="amber">Unconfirmed</Badge>
              <span className="text-[11px] text-haze">
                {candidate?.static_source ? 'Static-source flag' : 'Moving'}
              </span>
            </div>
            {candidate && (
              <p className="m-0 font-mono text-[11px] text-haze">
                {formatRa(candidate.position.ra)} · {formatDec(candidate.position.dec)}
              </p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {onSelectOnMap && (
              <Button variant="ghost" size="icon" aria-label="Select on map" onClick={onSelectOnMap}>
                <Crosshair className="size-4" aria-hidden="true" />
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              aria-label={copied === 'link' ? 'Link copied' : 'Copy link to candidate'}
              onClick={() => {
                const url = new URL(window.location.href);
                url.searchParams.set('sel', candidateId);
                void copy('link', url.toString());
              }}
            >
              {copied === 'link' ? (
                <Check className="size-4 text-accent" aria-hidden="true" />
              ) : (
                <Link2 className="size-4" aria-hidden="true" />
              )}
            </Button>
            <Button asChild variant="outline" size="sm" className="min-h-8 px-2.5 text-[11px]">
              <Link
                to={{
                  pathname: `/candidates/${encodeURIComponent(candidateId)}`,
                  search: `?f=${encodeURIComponent(field.field_id)}`,
                }}
                aria-label="Open full dossier"
              >
                Review dossier
                <FileText className="size-3.5" aria-hidden="true" />
              </Link>
            </Button>
            {onClose && (
              <Button variant="ghost" size="icon" aria-label="Close candidate detail" onClick={onClose}>
                <X className="size-4" aria-hidden="true" />
              </Button>
            )}
          </div>
        </header>
      )}

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto overscroll-contain p-4">
        {detail.isLoading && (
          <div className="flex flex-col gap-3" role="status">
            <p className="m-0 text-sm text-haze">Loading candidate detail…</p>
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}

        {detail.isError && <ErrorNote error={detail.error} onRetry={() => detail.refetch()} />}

        {candidate && (
          <>
            <section className="border-l-2 border-accent bg-accent-soft px-3 py-3" aria-labelledby="drawer-summary-heading">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 id="drawer-summary-heading" className="m-0 text-[15px] font-semibold">What we know so far</h3>
                <Badge tone="amber">Unconfirmed</Badge>
              </div>
              <p className="m-0 mt-2 text-[13px] leading-relaxed text-mist">
                {candidate.static_source
                  ? `This candidate is marked as a static-source flag across ${metrics?.baseline.toFixed(1) ?? '—'} days of stored positions.`
                  : `Candidate ${formatId(candidate.ordinal)} moved ${formatArcsec(metrics?.shiftArcsec ?? 0)} over ${metrics?.baseline.toFixed(1) ?? '—'} days.`}{' '}
                Review the original frames before any follow-up claim.
              </p>
            </section>

            <section className="flex flex-col gap-2" aria-labelledby="stack-heading">
              <SectionTitle id="stack-heading" aside={`${frames.length} frame${frames.length === 1 ? '' : 's'}`}>
                Stack versus originals
              </SectionTitle>

              <div className="grid gap-2 sm:grid-cols-2">
                <figure className="m-0 flex flex-col gap-1.5">
                  <Cutout
                    src={api.candidateStack(field.field_id, candidateId)}
                    alt={`Shift-and-stack of ${frames.length} frames for candidate ${formatId(candidate.ordinal)}`}
                    className="aspect-square"
                  />
                  <figcaption className="text-[11px] leading-snug text-haze">
                    Shift-and-stack of {frames.length} frames at {formatRate(metrics?.rate ?? 0)}.
                  </figcaption>
                </figure>

                <figure className="m-0 flex flex-col gap-1.5">
                  {current ? (
                    <Cutout
                      src={api.candidateCutout(field.field_id, candidateId, current.frame_ref)}
                      alt={`Original frame ${formatMjdDate(current.mjd)}, ${formatWavelength(
                        current.wavelength_um,
                      )}, detector ${current.detector}`}
                      className="aspect-square"
                      crosshair
                    />
                  ) : (
                    <div className="grid aspect-square place-items-center border border-dashed border-line-2 bg-canvas text-[11px] text-haze">
                      No original frames stored.
                    </div>
                  )}
                  <figcaption className="text-[11px] leading-snug text-haze">
                    {current
                      ? `Original · ${formatMjdDate(current.mjd)} · ${formatWavelength(
                          current.wavelength_um,
                        )} · D${current.detector}`
                      : 'No original frame selected.'}
                  </figcaption>
                </figure>
              </div>

              {frames.length > 0 && (
                <div className="flex flex-col gap-2 border border-line bg-canvas p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="flex items-center gap-1 text-[10px] text-haze">
                      Frame
                      <select
                        value={frameIndex}
                        onChange={(event) => {
                          setBlinking(false);
                          setFrameIndex(Number(event.target.value));
                        }}
                        className="min-h-7 rounded-sm border border-line bg-panel px-1.5 font-mono text-[10px] text-mist"
                        aria-label="Choose original frame"
                      >
                        {frames.map((frame, index) => (
                          <option key={frame.frame_ref} value={index}>
                            {formatMjdDate(frame.mjd)} · {formatWavelength(frame.wavelength_um)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <Button
                      variant="outline"
                      size="sm"
                      className="min-h-7 px-2 text-[10px]"
                      aria-pressed={blinking}
                      onClick={() => setBlinking((value) => !value)}
                      disabled={frames.length < 2}
                    >
                      {blinking ? 'Stop blink' : 'Blink first and last'}
                    </Button>
                    {copied === 'provenance' && <span className="text-[10px] text-accent">Copied</span>}
                  </div>

                  <div className="flex gap-1 overflow-x-auto pb-1" aria-label="Frame filmstrip">
                    {frames.map((frame, index) => {
                      const dimmed =
                        wavelengthRange !== null &&
                        !inWavelengthRange(frame.wavelength_um, wavelengthRange);
                      return (
                        <button
                          key={frame.frame_ref}
                          type="button"
                          onClick={() => {
                            setBlinking(false);
                            setFrameIndex(index);
                          }}
                          aria-pressed={index === frameIndex}
                          aria-label={`Show original frame ${index + 1} of ${frames.length}${
                            dimmed ? ', outside selected wavelength range' : ''
                          }`}
                          className={cn(
                            'relative size-11 shrink-0 cursor-pointer overflow-hidden border bg-panel',
                            index === frameIndex ? 'border-accent' : 'border-line hover:border-line-2',
                            dimmed && 'opacity-40',
                          )}
                        >
                          <img
                            src={api.candidateCutout(field.field_id, candidateId, frame.frame_ref)}
                            alt=""
                            loading="lazy"
                            className="h-full w-full object-cover"
                            onError={(event) => {
                              (event.currentTarget as HTMLImageElement).style.opacity = '0';
                            }}
                          />
                          <span className="absolute inset-x-0 bottom-0 bg-panel/85 text-center font-mono text-[8px] text-haze">
                            {index + 1}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </section>

            <section className="flex flex-col gap-2 border-t border-line pt-4" aria-labelledby="motion-heading">
              <SectionTitle id="motion-heading">Motion</SectionTitle>
              <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-[11px]">
                <div>
                  <dt className="text-haze">Rate</dt>
                  <dd className="m-0 font-mono text-mist">{formatRate(metrics?.rate ?? 0)}</dd>
                </div>
                <div>
                  <dt className="text-haze">Direction</dt>
                  <dd className="m-0 font-mono text-mist">{formatPositionAngle(metrics?.angle ?? 0)}</dd>
                </div>
                <div>
                  <dt className="text-haze">Baseline</dt>
                  <dd className="m-0 font-mono text-mist">
                    {metrics ? `${metrics.baseline.toFixed(1)} days` : '—'}
                  </dd>
                </div>
                <div>
                  <dt className="text-haze">Shift over baseline</dt>
                  <dd className="m-0 font-mono text-mist">
                    {formatArcsec(metrics?.shiftArcsec ?? 0)} ·{' '}
                    {metrics ? `${metrics.shiftPx.toFixed(1)} px` : '—'}
                  </dd>
                </div>
              </dl>
              <p className="m-0 text-[10px] leading-snug text-haze">
                Displayed motion in the sky view can be exaggerated; this is the measured shift over
                the stored frames.
              </p>
            </section>

            <section className="flex flex-col gap-2 border-t border-line pt-4" aria-labelledby="triage-heading">
              <div className="flex items-center gap-1.5">
                <Sparkles className="size-3.5 text-accent" aria-hidden="true" />
                <h3 id="triage-heading" className="m-0 text-[12px] font-semibold tracking-[0.02em]">
                  AI triage
                </h3>
              </div>
              <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-[11px]">
                <div>
                  <dt className="text-haze">Priority · {triageBasis}</dt>
                  <dd className="m-0 font-mono text-ink">{triageValue}</dd>
                </div>
                <div>
                  <dt className="text-haze">Model version</dt>
                  <dd className="m-0 truncate font-mono text-mist">
                    {candidate.model_score?.model_version ??
                      (rankingEnabled ? 'Not ranked' : 'Ranking unavailable')}
                  </dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-haze">Predicted category</dt>
                  <dd className="m-0 font-mono text-mist">
                    {classificationEnabled && candidate.predicted_category
                      ? candidate.predicted_category
                      : 'Not classified'}
                  </dd>
                </div>
              </dl>
              <p className="m-0 text-[10px] leading-snug text-haze">
                {rankingEnabled
                  ? 'A model score is a prediction trained on synthetic sources. It orders candidates for inspection; it is not a detection claim.'
                  : 'No ranking model is available on this server, so triage falls back to stack SNR ordering.'}
              </p>
            </section>

            <section className="flex flex-col gap-2 border-t border-line pt-4" aria-labelledby="scores-heading">
              <SectionTitle id="scores-heading">Scores</SectionTitle>
              <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-[11px]">
                <div>
                  <dt
                    className="text-haze"
                    title="Signal-to-noise of the combined image at this position along this path."
                  >
                    Stack SNR
                  </dt>
                  <dd className="m-0 font-mono text-mist">{formatSnr(candidate.stack_snr)}</dd>
                </div>
                {rankingEnabled && (
                  <div>
                    <dt className="text-haze">Model score</dt>
                    <dd className="m-0 font-mono text-mist">
                      {candidate.model_score ? formatScore(candidate.model_score.value) : 'Not ranked'}
                    </dd>
                  </div>
                )}
              </dl>
            </section>

            <section className="flex flex-col gap-2 border-t border-line pt-4" aria-labelledby="lightcurve-heading">
              <SectionTitle id="lightcurve-heading" aside="quick-look · not calibrated">
                Light curve
              </SectionTitle>
              {lightcurve.isLoading && <Skeleton className="h-32 w-full" />}
              {lightcurve.isError && (
                <p className="m-0 text-[12px] leading-snug text-haze">
                  No light curve is available for this candidate.
                </p>
              )}
              {lightcurve.data && (
                <LightcurveChart
                  lightcurve={lightcurve.data.data}
                  compact
                  wavelengthRange={wavelengthRange}
                />
              )}
            </section>

            <section className="flex flex-col gap-2 border-t border-line pt-4" aria-labelledby="spectrum-heading">
              <div className="flex items-center justify-between gap-2">
                <SectionTitle id="spectrum-heading" aside="SPHEREx 0.75–5.0 µm">
                  Spectrum
                </SectionTitle>
                <div className="flex overflow-hidden rounded-sm border border-line" role="group" aria-label="Spectrum view">
                  <button
                    type="button"
                    onClick={() => setSpectrumMode('102band')}
                    aria-pressed={spectrumMode === '102band'}
                    className={cn(
                      'min-h-7 px-2 text-[10px] whitespace-nowrap',
                      spectrumMode === '102band'
                        ? 'bg-accent text-accent-ink'
                        : 'text-haze hover:text-ink',
                    )}
                  >
                    102-Band Explorer
                  </button>
                  <button
                    type="button"
                    onClick={() => setSpectrumMode('sed')}
                    aria-pressed={spectrumMode === 'sed'}
                    className={cn(
                      'min-h-7 px-2 text-[10px] whitespace-nowrap',
                      spectrumMode === 'sed' ? 'bg-accent text-accent-ink' : 'text-haze hover:text-ink',
                    )}
                  >
                    Quick-Look SED
                  </button>
                </div>
              </div>

              {spectrum.isLoading && <Skeleton className="h-24 w-full" />}

              {!spectrum.isLoading && spectrumMode === '102band' && (
                <SpectralExplorer
                  candidate={candidate}
                  spectrum={spectrum.data?.data}
                  lightcurve={lightcurve.data?.data}
                />
              )}

              {!spectrum.isLoading && spectrumMode === 'sed' && (
                <>
                  {spectrum.isError && (
                    <p className="m-0 text-[12px] leading-snug text-haze">
                      No spectral energy distribution is available for this candidate.
                    </p>
                  )}
                  {spectrum.data && (
                    <SpectrumView
                      spectrum={spectrum.data.data}
                      wavelengthRange={wavelengthRange}
                      showMethodNote={false}
                    />
                  )}
                  <div className="mt-1 border-t border-line pt-2">
                    <h4 className="m-0 mb-1 text-[10px] font-semibold tracking-[0.12em] text-haze uppercase">
                      Wavelength coverage
                    </h4>
                    <WavelengthCoverage
                      frames={frames}
                      spectrum={spectrum.data?.data}
                      selectedWavelength={current?.wavelength_um ?? null}
                      selection={readOnlyWavelength ? null : wavelengthRange}
                      onSelectionChange={readOnlyWavelength ? undefined : setWavelengthRange}
                    />
                  </div>
                </>
              )}
            </section>

            <section className="flex flex-col gap-2 border-t border-line pt-4" aria-labelledby="provenance-heading">
              <div className="flex items-center justify-between gap-2">
                <h3 id="provenance-heading" className="m-0 text-[12px] font-semibold tracking-[0.02em]">
                  Provenance
                </h3>
                <Button
                  variant="ghost"
                  size="sm"
                  className="min-h-7 px-2 text-[10px]"
                  onClick={() => void copy('provenance', provenance)}
                >
                  <Copy className="size-3" aria-hidden="true" />
                  {copied === 'provenance' ? 'Copied' : 'Copy as text'}
                </Button>
              </div>
              <dl className="m-0 grid grid-cols-[100px_minmax(0,1fr)] gap-x-2 gap-y-1 font-mono text-[10px]">
                <dt className="text-haze">Candidate</dt>
                <dd className="m-0 truncate text-mist" title={candidate.candidate_id}>
                  {candidate.candidate_id}
                </dd>
                <dt className="text-haze">Job</dt>
                <dd className="m-0 truncate text-mist" title={candidate.job_id}>
                  {candidate.job_id}
                </dd>
                <dt className="text-haze">Release</dt>
                <dd className="m-0 text-mist">{formatRelease(field.params.release)}</dd>
                <dt className="text-haze">Reference MJD</dt>
                <dd className="m-0 text-mist">{candidate.reference_mjd}</dd>
                <dt className="text-haze">Frames</dt>
                <dd className="m-0 text-mist">
                  {frames.length > 0
                    ? `${formatMjdDate(frames[0].mjd)} to ${formatMjdDate(frames[frames.length - 1].mjd)}`
                    : 'None stored'}
                </dd>
              </dl>
              <div className="max-h-40 overflow-auto border border-line">
                <table className="w-full border-collapse text-left text-[10px]">
                  <caption className="sr-only">Frames used for this candidate</caption>
                  <thead className="bg-canvas text-haze">
                    <tr>
                      <th scope="col" className="px-2 py-1 font-medium">Date</th>
                      <th scope="col" className="px-2 py-1 font-medium">Wavelength</th>
                      <th scope="col" className="px-2 py-1 font-medium">Detector</th>
                    </tr>
                  </thead>
                  <tbody>
                    {frames.map((frame) => (
                      <tr key={frame.frame_ref} className="border-t border-line/70">
                        <td className="px-2 py-1 font-mono">{formatMjdDate(frame.mjd)}</td>
                        <td className="px-2 py-1 font-mono">{formatWavelength(frame.wavelength_um)}</td>
                        <td className="px-2 py-1 font-mono">D{frame.detector}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            <p className="m-0 flex items-start gap-1.5 text-[10px] leading-snug text-haze">
              <MousePointer2 className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              Originals are always kept beside the stack. Whether the source visibly moves is the
              evidence; this screen does not decide it for you.
            </p>
          </>
        )}
      </div>
    </div>
  );

  if (variant === 'pane') {
    return (
      <aside
        aria-label="Candidate detail"
        className={cn('flex min-h-0 flex-col border-l border-line bg-panel', className)}
      >
        {body}
      </aside>
    );
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Candidate detail"
      className={cn(
        'fixed inset-y-0 right-0 z-30 w-full max-w-[420px] border-l border-line bg-panel',
        className,
      )}
    >
      {body}
    </div>
  );
}
