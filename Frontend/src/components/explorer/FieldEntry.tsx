import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, Crosshair, Eye, X } from 'lucide-react';
import { api, usingMockApi } from '@/data/api';
import { ErrorNote } from '@/components/common/ErrorNote';
import { usePresets, useReleases } from '@/data/hooks';
import { useUrlState } from '@/state/urlState';
import { useUi } from '@/store/useUi';
import { formatDec, formatRa } from '@/lib/format';
import { parseCoordinates } from '@/lib/parseCoords';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { Preset } from '@/data/types';

const RADII = [0.05, 0.1, 0.25, 0.5];

const MOCK_PREVIEW_FRAMES: Record<string, { fieldId: string; frameRef: string }> = {
  tutorial: {
    fieldId: 'f_25faaae4bee9c18ab8c3ce82',
    frameRef: 'qr2.b8b71d02-0650-4a29-b47b-3b4b1cdbcad6D1',
  },
  'barnards-star': {
    fieldId: 'f_3c97b611b843ae10d439395a',
    frameRef: 'qr3.01a0eea8-5429-76cb-8620-14c2e95cd100D5',
  },
  'epsilon-indi-b': {
    fieldId: 'f_47a8f0fb315ca4961417999a',
    frameRef: 'qr3.01a0db40-df5f-76b5-b814-4458278f66dfD3',
  },
  'wise-0855': {
    fieldId: 'f_ec6ebe1a5e63a231d3d66893',
    frameRef: 'qr2.d9940807-520b-48ce-9bc8-372b7cdfe6beD2',
  },
  'rw-aur': {
    fieldId: 'f_6bc163f4091713c4a85d3b90',
    frameRef: 'qr3.01a0f0f0-e117-7913-a537-727a0fd9068dD5',
  },
  'r-dra': {
    fieldId: 'f_be719d6214cce7c417ef1b88',
    frameRef: 'qr3.01a0eea5-f3b9-7b55-b35d-9cb7f7537159D1',
  },
};

function presetPreview(preset: Preset): string | null {
  if (preset.thumbnail_url) return preset.thumbnail_url;
  if (!usingMockApi) return null;
  const frame = MOCK_PREVIEW_FRAMES[preset.name];
  return frame ? api.frameImage(frame.fieldId, frame.frameRef) : null;
}

function PresetCard({ preset, onOpen }: { preset: Preset; onOpen: (preset: Preset) => void }) {
  const preview = presetPreview(preset);
  return (
    <button
      type="button"
      onClick={() => onOpen(preset)}
      className="group flex cursor-pointer flex-col overflow-hidden border border-line bg-panel text-left transition-colors hover:border-accent focus-visible:border-accent focus-visible:outline-none"
    >
      <div className="relative aspect-[1.65] overflow-hidden bg-sky">
        {preview ? (
          <img
            src={preview}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover opacity-85 transition-transform duration-300 group-hover:scale-[1.03]"
          />
        ) : (
          <div className="field-preview-fallback h-full w-full" aria-hidden="true">
            <span className="field-preview-crosshair" />
          </div>
        )}
        <div className="absolute inset-x-0 bottom-0 flex items-center justify-between bg-gradient-to-t from-sky/95 to-transparent px-3 pb-2 pt-8 text-[11px] text-star">
          <span className="font-mono">{preset.release.toUpperCase()}</span>
          <span className="flex items-center gap-1 text-star/75">
            <Eye className="size-3" aria-hidden="true" />
            Open field
          </span>
        </div>
      </div>
      <div className="flex flex-col gap-2 p-4">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[16px] font-semibold text-ink">{preset.name}</span>
          <ArrowRight className="size-4 shrink-0 text-accent transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
        </div>
        <p className="m-0 line-clamp-3 min-h-[3.75rem] text-[13px] leading-relaxed text-mist">{preset.why}</p>
        <span className="font-mono text-[11px] text-haze">
          {formatRa(preset.ra)} · {formatDec(preset.dec)}
        </span>
      </div>
    </button>
  );
}

export function FieldEntry({ variant = 'landing' }: { variant?: 'landing' | 'dialog' }) {
  const presets = usePresets();
  const releases = useReleases();
  const { openField } = useUrlState();
  const close = useUi((state) => state.setFieldEntryOpen);
  const queryClient = useQueryClient();

  const [input, setInput] = useState('');
  const [radius, setRadius] = useState(0.1);
  const [release, setRelease] = useState<string>('qr2');
  const [error, setError] = useState<unknown>(null);

  const parsed = useMemo(() => parseCoordinates(input), [input]);
  const matchingPresets = useMemo(() => {
    const text = input.trim().toLowerCase();
    if (!text) return [];
    return (presets.data?.data.presets ?? []).filter((preset) => preset.name.includes(text));
  }, [input, presets.data]);

  const create = useMutation({
    mutationFn: api.createField,
    onSuccess: (envelope) => {
      const field = envelope.data;
      queryClient.invalidateQueries();
      openField({
        f: field.field_id,
        ra: field.params.ra,
        dec: field.params.dec,
        r: field.params.radius_deg,
        rel: field.params.release,
      });
      close(false);
    },
    onError: (cause) => setError(cause),
  });

  function openPreset(preset: Preset) {
    setError(null);
    create.mutate({ preset: preset.name });
  }

  function submitCoordinates() {
    if (!parsed) return;
    setError(null);
    create.mutate({ ra: parsed.ra, dec: parsed.dec, radius_deg: radius, release, search_pad: true });
  }

  const defaultRelease = release || releases.data?.data.releases.find((r) => r.enabled)?.id || 'qr2';

  const body = (
    <div className={cn('flex flex-col gap-8', variant === 'landing' && 'mx-auto w-full max-w-6xl')}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="app-kicker">Sky Explorer</div>
          <h1 className="m-0 mt-3 max-w-[18ch] text-4xl font-bold tracking-[-0.055em] sm:text-6xl sm:leading-[0.95]">
            {variant === 'landing' ? 'Find a faint mover in real sky data' : 'Open another field'}
          </h1>
          <p className="m-0 mt-4 max-w-[58ch] text-[15px] leading-relaxed text-mist">
            Start with a field, watch the original SPHEREx frames blink through time, then open one
            unconfirmed candidate for a closer look.
          </p>
        </div>
        {variant === 'dialog' && (
          <Button variant="ghost" size="icon" aria-label="Close field entry" onClick={() => close(false)}>
            <X className="size-4" aria-hidden="true" />
          </Button>
        )}
      </div>

      <section className="flex flex-col gap-4" aria-labelledby="preset-heading">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <div>
            <h2 id="preset-heading" className="m-0 text-xl font-semibold tracking-[-0.02em]">
              Start with a field
            </h2>
            <p className="m-0 mt-1 text-[13px] text-haze">Choose a patch with a reason to look twice.</p>
          </div>
          <span className="text-[12px] text-haze">
            {usingMockApi
              ? 'Demo data · stored results'
              : 'Open a preset to load its field and observations.'}
          </span>
        </div>
        {presets.isLoading && (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" role="status" aria-label="Loading fields">
            {[0, 1, 2].map((item) => (
              <div key={item} className="h-[270px] animate-pulse border border-line bg-panel" />
            ))}
          </div>
        )}
        {presets.isError && <ErrorNote error={presets.error} onRetry={() => presets.refetch()} />}
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {(presets.data?.data.presets ?? []).map((preset) => (
            <PresetCard key={preset.name} preset={preset} onOpen={openPreset} />
          ))}
        </div>
      </section>

      <section
        className="flex flex-col gap-4 border-t border-line pt-5"
        aria-labelledby="coord-heading"
      >
        <div className="flex items-baseline justify-between gap-3">
          <h2 id="coord-heading" className="m-0 text-base font-semibold">
            Or enter coordinates
          </h2>
          <span className="text-[12px] text-haze">Secondary path · ICRS degrees, or 10h52m04s +57d28m11s</span>
        </div>
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_180px_180px_auto] lg:items-end">
          <label className="flex flex-col gap-1.5 text-[12px] text-mist">
            Position
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  if (parsed) submitCoordinates();
                  else if (matchingPresets.length === 1) openPreset(matchingPresets[0]);
                }
              }}
              placeholder="e.g. 156.09 -41.64"
              aria-invalid={input.length > 0 && !parsed && matchingPresets.length === 0}
              className="min-h-11 rounded-none border border-line-2 bg-bar px-3 font-mono text-sm text-ink placeholder:text-haze focus:border-accent focus:outline-none"
            />
          </label>
          <label className="flex flex-col gap-1.5 text-[12px] text-mist">
            Radius
            <select
              value={radius}
              onChange={(event) => setRadius(Number(event.target.value))}
              className="min-h-11 rounded-none border border-line-2 bg-bar px-3 text-sm text-ink focus:border-accent focus:outline-none"
            >
              {RADII.map((value) => (
                <option key={value} value={value}>
                  {value}° ({`${(value * 60).toFixed(0)}′`})
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5 text-[12px] text-mist">
            Release
            <select
              value={defaultRelease}
              onChange={(event) => setRelease(event.target.value)}
              className="min-h-11 rounded-none border border-line-2 bg-bar px-3 text-sm text-ink focus:border-accent focus:outline-none"
            >
              {(releases.data?.data.releases ?? []).map((item) => (
                <option key={item.id} value={item.id} disabled={!item.enabled}>
                  {item.id.toUpperCase()} · {item.label}
                </option>
              ))}
            </select>
          </label>
          <Button onClick={submitCoordinates} disabled={!parsed || create.isPending}>
            <Crosshair className="size-4" aria-hidden="true" />
            {create.isPending ? 'Opening…' : 'Open field'}
          </Button>
        </div>

        {input && !parsed && matchingPresets.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {matchingPresets.map((preset) => (
              <Button key={preset.name} size="sm" variant="outline" onClick={() => openPreset(preset)}>
                {preset.name}
              </Button>
            ))}
          </div>
        )}

        {parsed && (
          <p className="m-0 font-mono text-[12px] text-teal">
            Parsed {formatRa(parsed.ra)} · {formatDec(parsed.dec)} · r {radius}° · {defaultRelease.toUpperCase()}
          </p>
        )}

        {usingMockApi && (
          <p className="m-0 text-[12px] leading-snug text-haze">
            Mock mode serves the six demo presets. To search an arbitrary position, run the backend
            with <code className="font-mono">ORB_OFFLINE=1</code> and set{' '}
            <code className="font-mono">VITE_API_URL</code>.
          </p>
        )}

        {error !== null && <ErrorNote error={error} compact />}
      </section>

      <p className="m-0 text-[12px] leading-relaxed text-haze">
        Name lookup is not part of the current API contract; enter coordinates. Every candidate the
        search returns is unconfirmed.
      </p>
    </div>
  );

  if (variant === 'dialog') {
    return (
      <div
        className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/70 p-6 pt-[7vh]"
        role="dialog"
        aria-modal="true"
        aria-label="Open a field"
        onClick={(event) => {
          if (event.target === event.currentTarget) close(false);
        }}
      >
        <div className="w-full max-w-5xl rounded-none border border-line-2 bg-panel p-6 backdrop-blur-xl">
          {body}
        </div>
      </div>
    );
  }

  return <div className="mx-auto w-full max-w-6xl px-6 py-10">{body}</div>;
}
