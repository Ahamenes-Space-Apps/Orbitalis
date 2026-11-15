import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ListFilter, RefreshCw, X } from 'lucide-react';
import { api } from '@/data/api';
import { ApiError } from '@/data/errors';
import {
  useCandidate,
  useCandidates,
  useCapabilities,
  useField,
  useFieldLightcurve,
  useFieldStream,
  useFrames,
  useOverlays,
  useSpectrum,
} from '@/data/hooks';
import type { Overlay } from '@/data/types';
import { formatDec, formatMjdDate, formatRa, formatRelease, formatWavelength } from '@/lib/format';
import type { SkyPos } from '@/lib/coords';
import type { WavelengthRange } from '@/lib/wavelength';
import { useSearchPreservingPath, useUrlState, type LayerName } from '@/state/urlState';
import { useUi } from '@/store/useUi';
import { ErrorNote } from '@/components/common/ErrorNote';
import { Skeleton } from '@/components/common/Primitives';
import { StatusChip } from '@/components/common/StatusChip';
import { LightcurveChart } from '@/components/charts/LightcurveChart';
import { SpectrumView } from '@/components/charts/SpectrumView';
//import { WavelengthStrip } from '@/components/charts/WavelengthStrip';
import { CandidateDrawer } from '@/components/explorer/CandidateDrawer';
import { CandidateQueue } from '@/components/explorer/CandidateQueue';
import { FieldEntry } from '@/components/explorer/FieldEntry';
import { FrameScrubber } from '@/components/explorer/FrameScrubber';
import { SkyView, type TrackPoint } from '@/components/explorer/SkyView';
import { SpectralTray } from '@/components/explorer/SpectralTray';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

function FieldBuilder({
  ra,
  dec,
  radius,
  release,
  children,
}: {
  ra: number;
  dec: number;
  radius: number;
  release: string | null;
  children: (fieldId: string) => ReactNode;
}) {
  const { update } = useUrlState();
  const started = useRef(false);
  const create = useMutation({
    mutationFn: api.createField,
    onSuccess: (envelope) => update({ f: envelope.data.field_id }, { replace: true }),
  });

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    create.mutate({ ra, dec, radius_deg: radius, release, search_pad: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (create.isError) {
    return (
      <div className="mx-auto w-full max-w-3xl px-6 py-10">
        <ErrorNote
          error={create.error}
          onRetry={() => {
            create.reset();
            create.mutate({ ra, dec, radius_deg: radius, release, search_pad: true });
          }}
        />
        <p className="mt-4 text-[12px] leading-relaxed text-haze">
          The field is built from the URL parameters, so this link stays usable. Nothing else is
          lost.
        </p>
      </div>
    );
  }

  if (!create.data) {
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-6 py-10" role="status">
        <p className="m-0 text-sm text-haze">Rebuilding this view from the URL…</p>
        <Skeleton className="h-40 w-full" />
        <p className="m-0 font-mono text-[11px] text-haze">
          RA {formatRa(ra)} · Dec {formatDec(dec)} · r {radius}° · {formatRelease(release)}
        </p>
      </div>
    );
  }

  return <>{children(create.data.data.field_id)}</>;
}

function SkyPointPanel({
  fieldId,
  point,
  wavelengthRange = null,
  onClose,
}: {
  fieldId: string;
  point: SkyPos;
  wavelengthRange?: WavelengthRange | null;
  onClose: () => void;
}) {
  const lightcurve = useFieldLightcurve(fieldId, point.ra, point.dec);
  const spectrum = useSpectrum(fieldId, point.ra, point.dec);
  const [copied, setCopied] = useState(false);

  return (
    <div className="absolute right-3 bottom-3 z-20 max-h-[calc(100%-24px)] w-[min(360px,calc(100%-24px))] overflow-y-auto overscroll-contain rounded-lg border border-line bg-panel p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="m-0 text-[12px] font-semibold">Sky position</h2>
          <p className="m-0 font-mono text-[10px] text-haze">
            {formatRa(point.ra)} · {formatDec(point.dec)}
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="size-7 min-h-0"
          aria-label="Close sky position"
          onClick={onClose}
        >
          <X className="size-3.5" aria-hidden="true" />
        </Button>
      </div>

      <div className="mt-2 flex flex-wrap gap-1.5">
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            void navigator.clipboard
              .writeText(`${point.ra.toFixed(6)}, ${point.dec.toFixed(6)}`)
              .then(() => setCopied(true))
              .catch(() => setCopied(false));
          }}
        >
          {copied ? 'Copied' : 'Copy coordinates'}
        </Button>
      </div>

      <div className="mt-3 flex flex-col gap-2">
        <h3 className="m-0 text-[11px] font-semibold tracking-[0.12em] text-haze uppercase">
          Light curve here
        </h3>
        {lightcurve.isLoading && <Skeleton className="h-24 w-full" />}
        {lightcurve.isError && (
          <p className="m-0 text-[11px] leading-snug text-haze">
            No light curve could be measured at this position.
          </p>
        )}
        {lightcurve.data && (
          <LightcurveChart lightcurve={lightcurve.data.data} compact wavelengthRange={wavelengthRange} />
        )}
      </div>

      <div className="mt-3 border-t border-line pt-2">
        <h3 className="m-0 mb-1 text-[11px] font-semibold tracking-[0.12em] text-haze uppercase">
          Spectrum
        </h3>
        {spectrum.data ? (
          <SpectrumView spectrum={spectrum.data.data} wavelengthRange={wavelengthRange} />
        ) : (
          <p className="m-0 text-[11px] text-haze">Checking the spectrum…</p>
        )}
      </div>
    </div>
  );
}

function downloadJson(filename: string, payload: unknown) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: 'application/json;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function ExplorerWorkspace({ fieldId }: { fieldId: string }) {
  const { values, update, remove } = useUrlState();
  const navigate = useNavigate();
  const preserve = useSearchPreservingPath();
  const fieldQuery = useField(fieldId);
  const capabilities = useCapabilities();
  const framesQuery = useFrames(fieldId);
  const [frameOrder, setFrameOrder] = useState<'time' | 'wavelength'>('time');
  const [dockMode, setDockMode] = useState<'epoch' | 'trails' | 'diff'>('epoch');
  const [skyPoint, setSkyPoint] = useState<SkyPos | null>(null);
  const [wavelengthRange, setWavelengthRange] = useState<WavelengthRange | null>(null);
  const [resetSignal, setResetSignal] = useState(0);
  const [queueOpen, setQueueOpen] = useState(false);
  const queryClient = useQueryClient();

  const field = fieldQuery.data?.data;
  const frames = framesQuery.data?.data.frames ?? [];
  const rankEnabled = Boolean(capabilities.data?.data.ranking.enabled);
  const basis = rankEnabled ? 'model_score' : 'stack_snr';
  const candidatesQuery = useCandidates(fieldId, basis);
  const candidates = candidatesQuery.data?.data.candidates ?? [];
  const modelVersion =
    candidates.find((candidate) => candidate.model_score)?.model_score?.model_version ??
    capabilities.data?.data.ranking.model_version ??
    null;

  const known = useOverlays(fieldId, 'known', values.layers.includes('known'));
  const hypothesis = useOverlays(fieldId, 'hypothesis', values.layers.includes('hypothesis'));

  const active = field?.status === 'queued' || field?.status === 'running';
  useFieldStream(fieldId, Boolean(active));

  const orderedFrames = useMemo(
    () =>
      [...frames].sort((a, b) =>
        frameOrder === 'time' ? a.mjd - b.mjd : a.wavelength_um - b.wavelength_um,
      ),
    [frameOrder, frames],
  );
  const selectedFrame = useMemo(() => {
    if (orderedFrames.length === 0) return null;
    return orderedFrames.find((frame) => frame.frame_ref === values.t) ?? orderedFrames[0];
  }, [orderedFrames, values.t]);
  const epochIndex = Math.max(
    0,
    orderedFrames.findIndex((frame) => frame.frame_ref === selectedFrame?.frame_ref),
  );

  useEffect(() => {
    if (!selectedFrame) return;
    if (values.t !== selectedFrame.frame_ref) update({ t: selectedFrame.frame_ref }, { replace: true });
  }, [selectedFrame, update, values.t]);

  const selectedId = values.sel;
  const selectedDetail = useCandidate(selectedId);
  const selectedTrack = useMemo<TrackPoint[] | null>(() => {
    const detailFrames = selectedDetail.data?.data.frames;
    if (!detailFrames?.length) return null;
    const points: TrackPoint[] = [];
    for (const frame of detailFrames) {
      if (frame.predicted_ra == null || frame.predicted_dec == null) continue;
      if (!Number.isFinite(frame.predicted_ra) || !Number.isFinite(frame.predicted_dec)) continue;
      points.push({ mjd: frame.mjd, ra: frame.predicted_ra, dec: frame.predicted_dec });
    }
    points.sort((a, b) => a.mjd - b.mjd);
    return points.length > 0 ? points : null;
  }, [selectedDetail.data]);
  const drawerOpen = useUi((state) => state.drawerOpen);
  const setDrawerOpen = useUi((state) => state.setDrawerOpen);
  const hoveredId = useUi((state) => state.hoveredCandidateId);
  const setHovered = useUi((state) => state.setHoveredCandidateId);
  const playing = useUi((state) => state.playing);
  const setPlaying = useUi((state) => state.setPlaying);
  const speed = useUi((state) => state.speed);
  const setSpeed = useUi((state) => state.setSpeed);
  const showFlags = useUi((state) => state.showFlags);
  const setShowFlags = useUi((state) => state.setShowFlags);
  const showGrid = useUi((state) => state.showGrid);
  const setShowGrid = useUi((state) => state.setShowGrid);
  const epochCollapsed = useUi((state) => state.epochCollapsed);
  const setEpochCollapsed = useUi((state) => state.setEpochCollapsed);
  const trayCollapsed = useUi((state) => state.trayCollapsed);
  const setTrayCollapsed = useUi((state) => state.setTrayCollapsed);

  useEffect(() => {
    if (selectedId) setDrawerOpen(true);
  }, [selectedId, setDrawerOpen]);

  useEffect(() => {
    if (selectedId) {
      const candidate = candidates.find((item) => item.candidate_id === selectedId);
      if (candidate) {
        document.title = `ORBITALIS · ${candidate.ordinal ? `#${String(candidate.ordinal).padStart(3, '0')}` : 'Candidate'}`;
      }
    }
  }, [candidates, selectedId]);

  useEffect(() => {
    setWavelengthRange(null);
  }, [fieldId]);

  const toggleLayer = (layer: LayerName) => {
    const next = values.layers.includes(layer)
      ? values.layers.filter((item) => item !== layer)
      : [...values.layers, layer];
    update({ layers: next });
  };

  const chooseDockMode = (mode: 'epoch' | 'trails' | 'diff') => {
    if (mode === 'diff') return;
    setDockMode(mode);
    if (mode === 'trails' && !values.layers.includes('trails')) toggleLayer('trails');
  };

  const rebuild = useMutation({
    mutationFn: () =>
      api.createField({
        ra: field?.params.ra ?? values.ra ?? undefined,
        dec: field?.params.dec ?? values.dec ?? undefined,
        radius_deg: field?.params.radius_deg ?? values.r,
        release: field?.params.release ?? values.rel,
        search_pad: true,
      }),
    onSuccess: (envelope) => {
      update({ f: envelope.data.field_id }, { replace: true });
      void queryClient.invalidateQueries();
    },
  });

  if (fieldQuery.isError) {
    const missing = fieldQuery.error instanceof ApiError && fieldQuery.error.code === 'ORB-API-002';
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-6 py-10">
        <ErrorNote error={fieldQuery.error} onRetry={() => fieldQuery.refetch()} />
        {missing && values.ra !== null && values.dec !== null && (
          <div className="flex flex-col gap-2 rounded-lg border border-line bg-panel p-4">
            <p className="m-0 text-sm text-mist">
              This field is no longer on the server. Rebuild it from the URL parameters.
            </p>
            <div>
              <Button onClick={() => rebuild.mutate()} disabled={rebuild.isPending}>
                <RefreshCw className={cn('size-4', rebuild.isPending && 'animate-spin')} aria-hidden="true" />
                {rebuild.isPending ? 'Rebuilding…' : 'Rebuild this view'}
              </Button>
            </div>
            {rebuild.isError && <ErrorNote error={rebuild.error} compact />}
          </div>
        )}
      </div>
    );
  }

  if (!field) {
    return (
      <div className="flex flex-1 flex-col gap-3 p-6" role="status">
        <p className="m-0 text-sm text-haze">Loading the field…</p>
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const exportDossier = () => {
    const selectedCandidate =
      candidates.find((candidate) => candidate.candidate_id === selectedId) ?? candidates[0] ?? null;
    downloadJson(`orbitalis-${field.field_id}-dossier.json`, {
      generated_at: new Date().toISOString(),
      note: 'Candidates are unconfirmed. Independent follow-up is required.',
      field,
      frames: orderedFrames,
      candidate: selectedCandidate,
      candidates,
    });
  };

  const queue = (onClose?: () => void) => (
    <CandidateQueue
      candidates={candidates}
      selectedId={selectedId}
      basis={basis}
      modelVersion={modelVersion}
      loading={candidatesQuery.isLoading}
      error={candidatesQuery.error}
      canSearch={field.status === 'completed'}
      layers={values.layers}
      onToggleLayer={toggleLayer}
      showGrid={showGrid}
      onShowGrid={setShowGrid}
      onSelect={(candidateId) => {
        setDrawerOpen(true);
        update({ sel: candidateId });
        onClose?.();
      }}
      onHover={setHovered}
      onRunSearch={() => navigate(preserve('/candidates'))}
      onExport={exportDossier}
      canExport={candidates.length > 0}
      onClose={onClose}
    />
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-transparent">
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <aside className="hidden w-[328px] shrink-0 flex-col border-r border-line bg-panel lg:flex">
          {queue()}
        </aside>

        <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="relative min-h-0 flex-1" aria-label="Sky explorer">
            {framesQuery.isError && (
              <div className="absolute inset-0 z-10 grid place-items-center bg-canvas/80 p-6">
                <ErrorNote error={framesQuery.error} onRetry={() => framesQuery.refetch()} />
              </div>
            )}

            <SkyView
              field={field}
              frames={frames}
              candidates={candidates}
              selectedId={selectedId}
              hoveredId={hoveredId}
              frame={selectedFrame}
              view={values.view}
              sys={values.sys}
              layers={values.layers}
              exag={values.exag}
              showFlags={showFlags}
              showGrid={showGrid}
              resetSignal={resetSignal}
              known={known.data?.data as Overlay | undefined}
              hypothesis={hypothesis.data?.data as Overlay | undefined}
              onSelect={(candidateId) => {
                if (candidateId) {
                  setDrawerOpen(true);
                  update({ sel: candidateId });
                } else {
                  remove(['sel']);
                }
              }}
              onHover={setHovered}
              onCursor={(position) => useUi.getState().setCursor(position)}
              onSkyClick={(position) => setSkyPoint(position)}
              track={selectedTrack}
            />

            {/* Compact epoch HUD, top-left over the sky. */}
            <div className="pointer-events-none absolute top-3 left-3 z-10 flex max-w-[calc(100%-24px)] flex-col gap-1">
              <div className="pointer-events-auto flex w-fit flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-sky-line/70 bg-sky/85 px-2.5 py-1.5 text-[10px] text-star">
                <StatusChip status={field.status} className="border-sky-line bg-transparent text-star" />
                <span className="font-mono whitespace-nowrap">
                  {selectedFrame
                    ? `E${epochIndex + 1} / E${orderedFrames.length} · ${formatMjdDate(selectedFrame.mjd)} · ${formatWavelength(selectedFrame.wavelength_um)}`
                    : 'No epoch stored'}
                </span>
              </div>
              <div className="pointer-events-auto flex w-fit flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[10px] text-star/80">
              {/**  <span className="rounded-sm border border-sky-line/60 bg-sky/70 px-2 py-1">
                  {frames.length} frame{frames.length === 1 ? '' : 's'} · {candidates.length} candidate
                  {candidates.length === 1 ? '' : 's'}
                </span> */}
              
                <span className="rounded-sm border border-sky-line/60 bg-sky/70 px-2 py-1">
                  {formatRa(field.grid.ra)} · {formatDec(field.grid.dec)}
                </span>
                {active && (
                  <span className="rounded-sm border border-sky-line/60 bg-sky/70 px-2 py-1">
                    {field.frames_done} stored
                  </span>
                )}
              </div>
{/** {frames.length > 0 && (
                <div className="dark pointer-events-auto w-fit min-w-[72px] max-w-full rounded-md border border-sky-line/70 bg-sky/90 px-2 py-2 shadow-lg backdrop-blur">
                  <WavelengthStrip
                    frames={frames}
                    selectedWavelength={selectedFrame?.wavelength_um ?? null}
                    selection={wavelengthRange}
                    onSelectionChange={setWavelengthRange}
                  />
                </div>
              )} */}
              
            </div>

            {skyPoint && (
              <SkyPointPanel
                fieldId={fieldId}
                point={skyPoint}
                wavelengthRange={wavelengthRange}
                onClose={() => setSkyPoint(null)}
              />
            )}
          </div>

          <FrameScrubber
            frames={orderedFrames}
            selectedRef={selectedFrame?.frame_ref ?? null}
            order={frameOrder}
            playing={playing}
            speed={speed}
            wavelengthRange={wavelengthRange}
            collapsed={epochCollapsed}
            mode={dockMode}
            onCollapsed={setEpochCollapsed}
            onMode={chooseDockMode}
            onOrder={setFrameOrder}
            onSelect={(frameRef) => update({ t: frameRef })}
            onPlaying={setPlaying}
            onSpeed={setSpeed}
            onResetView={() => {
              setResetSignal((value) => value + 1);
              setSkyPoint(null);
            }}
          />

          <SpectralTray
            frames={orderedFrames}
            selectedRef={selectedFrame?.frame_ref ?? null}
            collapsed={trayCollapsed}
            onToggle={() => setTrayCollapsed(!trayCollapsed)}
            onSelectWavelength={(wavelength) => {
              const match = orderedFrames.reduce<(typeof orderedFrames)[number] | null>(
                (best, frame) =>
                  !best || Math.abs(frame.wavelength_um - wavelength) < Math.abs(best.wavelength_um - wavelength)
                    ? frame
                    : best,
                null,
              );
              if (match) update({ t: match.frame_ref });
            }}
          />
        </div>

        {selectedId && drawerOpen && (
          <>
            <button
              type="button"
              tabIndex={-1}
              aria-hidden="true"
              className="fixed inset-0 z-20 cursor-default bg-black/50 lg:hidden"
              onClick={() => setDrawerOpen(false)}
            />
            <CandidateDrawer
              candidateId={selectedId}
              field={field}
              variant="pane"
              className="fixed inset-y-0 right-0 z-30 w-full max-w-[380px] border-l border-line bg-panel lg:static lg:z-auto lg:w-[356px] lg:max-w-none lg:shrink-0"
              wavelengthRange={wavelengthRange}
              onWavelengthRange={setWavelengthRange}
              onClose={() => setDrawerOpen(false)}
              onSelectOnMap={() => {
                setSkyPoint(null);
                setDrawerOpen(false);
              }}
            />
          </>
        )}
      </div>

      {/* Mobile dock: the candidate rail and sky-layer switches are hidden below lg. */}
      <div className="flex shrink-0 items-center justify-between gap-2 border-t border-line bg-panel px-3 py-1.5 text-[10px] text-haze lg:hidden">
        <Button variant="outline" size="sm" onClick={() => setQueueOpen(true)}>
          <ListFilter className="size-3.5" aria-hidden="true" />
          Candidates ({candidates.length})
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setShowFlags(!showFlags)} aria-pressed={showFlags}>
          {showFlags ? 'Hide flags' : 'Show flags'}
        </Button>
      </div>

      {queueOpen && (
        <div
          className="fixed inset-0 z-40 flex lg:hidden"
          role="dialog"
          aria-modal="true"
          aria-label="Candidates and sky layers"
        >
          <button
            type="button"
            className="absolute inset-0 cursor-default bg-black/60"
            aria-label="Close candidate queue"
            onClick={() => setQueueOpen(false)}
          />
          <div className="relative z-10 ml-auto flex h-full w-[min(360px,90vw)] flex-col border-l border-line bg-panel shadow-2xl">
            {queue(() => setQueueOpen(false))}
          </div>
        </div>
      )}
    </div>
  );
}

export function ExplorerPage() {
  const { values } = useUrlState();

  if (values.f) return <ExplorerWorkspace fieldId={values.f} />;

  if (values.ra !== null && values.dec !== null) {
    return (
      <FieldBuilder
        key={`${values.ra},${values.dec}`}
        ra={values.ra}
        dec={values.dec}
        radius={values.r}
        release={values.rel}
      >
        {(fieldId) => <ExplorerWorkspace fieldId={fieldId} />}
      </FieldBuilder>
    );
  }

  return <FieldEntry variant="landing" />;
}
