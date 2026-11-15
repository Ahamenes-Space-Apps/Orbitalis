import { useEffect, useMemo, useState } from 'react';
import { Download, Search, X } from 'lucide-react';
import type { CandidateSummary, RankBasis } from '@/data/types';
import { formatId, formatRate } from '@/lib/format';
import { useUi } from '@/store/useUi';
import type { LayerName } from '@/state/urlState';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Slider } from '@/components/ui/slider';
import { Switch } from '@/components/ui/switch';
import { cn } from '@/lib/utils';

type Filter = 'all' | 'moving' | 'flux' | 'top';

const LAYERS: Array<{ id: LayerName; label: string }> = [
  { id: 'moving', label: 'Moving' },
  { id: 'static', label: 'Static flag' },
  { id: 'trails', label: 'Trails' },
  { id: 'known', label: 'Known sources' },
  { id: 'hypothesis', label: 'Hypothesis zones' },
];

interface CandidateQueueProps {
  candidates: CandidateSummary[];
  selectedId: string | null;
  basis: RankBasis;
  modelVersion: string | null;
  loading: boolean;
  error: unknown;
  canSearch: boolean;
  layers: LayerName[];
  onToggleLayer: (layer: LayerName) => void;
  showGrid: boolean;
  onShowGrid: (show: boolean) => void;
  onSelect: (candidateId: string) => void;
  onHover: (candidateId: string | null) => void;
  onRunSearch: () => void;
  onExport: () => void;
  canExport: boolean;
  showSkyControls?: boolean;
  onClose?: () => void;
}

export function CandidateQueue({
  candidates,
  selectedId,
  basis,
  modelVersion,
  loading,
  error,
  canSearch,
  layers,
  onToggleLayer,
  showGrid,
  onShowGrid,
  onSelect,
  onHover,
  onRunSearch,
  onExport,
  canExport,
  showSkyControls = true,
  onClose,
}: CandidateQueueProps) {
  const sortBy = useUi((state) => state.sortBy);
  const setSortBy = useUi((state) => state.setSortBy);
  const hoveredId = useUi((state) => state.hoveredCandidateId);
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [minScore, setMinScore] = useState(0);

  const scoreOf = (candidate: CandidateSummary) =>
    basis === 'model_score' ? (candidate.model_score?.value ?? 0) : candidate.stack_snr;

  const priorityMax =
    basis === 'model_score'
      ? 1
      : Math.max(
          1,
          ...candidates.map((candidate) => Math.ceil(candidate.stack_snr)),
        );

  useEffect(() => {
    setMinScore(0);
  }, [basis]);

  const counts = useMemo(
    () => ({
      all: candidates.length,
      moving: candidates.filter((candidate) => !candidate.static_source).length,
      flux: candidates.filter((candidate) => candidate.snr_calibrated === true).length,
      top: Math.min(10, candidates.length),
    }),
    [candidates],
  );

  const ordered = useMemo(() => {
    const text = query.trim().toLowerCase();
    const rows = candidates
      .filter((candidate) => {
        if (filter === 'moving' && candidate.static_source) return false;
        if (filter === 'flux' && candidate.snr_calibrated !== true) return false;
        if (scoreOf(candidate) < minScore) return false;
        if (!text) return true;
        return (
          candidate.candidate_id.toLowerCase().includes(text) ||
          formatId(candidate.ordinal).toLowerCase().includes(text)
        );
      })
      .sort((a, b) => {
        if (sortBy === 'rate') {
          return (
            Math.hypot(b.motion.mu_e_arcsec_per_day, b.motion.mu_n_arcsec_per_day) -
            Math.hypot(a.motion.mu_e_arcsec_per_day, a.motion.mu_n_arcsec_per_day)
          );
        }
        if (sortBy === 'ordinal') return (a.ordinal ?? 0) - (b.ordinal ?? 0);
        return scoreOf(b) - scoreOf(a) || (a.ordinal ?? 0) - (b.ordinal ?? 0);
      });
    return filter === 'top' ? rows.slice(0, 10) : rows;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basis, candidates, filter, minScore, query, sortBy]);

  const subtitle =
    basis === 'model_score'
      ? `Ranked by model score${modelVersion ? ` (${modelVersion})` : ''}. Unconfirmed.`
      : 'Ranked by stack SNR. Unconfirmed.';

  const filters: Array<{ id: Filter; label: string; count: number }> = [
    { id: 'all', label: 'All', count: counts.all },
    { id: 'moving', label: 'Moving', count: counts.moving },
    { id: 'flux', label: 'Flux Δ', count: counts.flux },
    { id: 'top', label: 'Top', count: counts.top },
  ];

  const clearFilters = () => {
    setFilter('all');
    setQuery('');
    setMinScore(0);
  };

  return (
    <section aria-label="Candidate queue" className="flex min-h-0 w-full flex-1 flex-col overflow-hidden">
      <header className="flex shrink-0 flex-col gap-1 border-b border-line px-3 py-2.5">
        <div className="flex items-center gap-2">
          <h2 className="m-0 truncate text-[13px] font-semibold tracking-[0.02em] text-ink">
            Candidates
          </h2>
          <Badge tone="amber">Unconfirmed</Badge>
          <span className="ml-auto shrink-0 font-mono text-[10px] text-haze">
            {counts.all} total
          </span>
          {onClose && (
            <Button
              variant="ghost"
              size="icon"
              className="size-7 min-h-0 shrink-0"
              aria-label="Close candidate queue"
              onClick={onClose}
            >
              <X className="size-3.5" aria-hidden="true" />
            </Button>
          )}
        </div>
        <p className="m-0 text-[11px] leading-snug text-haze" data-testid="queue-subtitle">
          {subtitle}
        </p>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-3 py-3">
        <label className="relative flex items-center">
          <Search className="pointer-events-none absolute left-2 size-3.5 text-haze" aria-hidden="true" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search candidate id or #"
            aria-label="Search candidates"
            className="min-h-8 w-full rounded-sm border border-line bg-canvas pr-2 pl-7 font-mono text-[11px] text-ink placeholder:text-haze focus:border-accent focus:outline-none"
          />
        </label>

        <div
          className="flex min-w-0 overflow-hidden rounded-sm border border-line"
          role="group"
          aria-label="Filter candidates"
        >
          {filters.map((item) => (
            <Button
              key={item.id}
              variant="segment"
              size="sm"
              className="min-w-0 flex-1 gap-1 px-1.5 text-[10px] whitespace-nowrap"
              data-active={filter === item.id}
              aria-pressed={filter === item.id}
              onClick={() => setFilter(item.id)}
            >
              <span className="truncate">{item.label}</span>
              <span className="font-mono text-[9px] text-haze">{item.count}</span>
            </Button>
          ))}
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] text-mist">Min priority score</span>
            <span className="font-mono text-[10px] text-accent">
              {basis === 'model_score' ? minScore.toFixed(2) : minScore.toFixed(1)}
            </span>
          </div>
          <Slider
            min={0}
            max={priorityMax}
            step={basis === 'model_score' ? 0.05 : 0.5}
            value={[minScore]}
            onValueChange={([value]) => setMinScore(value)}
            thumbLabels={['Minimum priority score']}
          />
        </div>

        <label className="flex items-center justify-between gap-2 text-[11px] text-mist">
          Sort
          <select
            value={sortBy}
            onChange={(event) => setSortBy(event.target.value as typeof sortBy)}
            className="min-h-7 rounded-sm border border-line bg-canvas px-1.5 font-mono text-[10px] text-mist"
            aria-label="Sort candidates"
          >
            <option value="rank">Rank</option>
            <option value="rate">Rate</option>
            <option value="ordinal">Ordinal</option>
          </select>
        </label>

        {loading && <p className="m-0 px-1 text-[12px] text-haze">Loading candidates…</p>}
        {Boolean(error) && (
          <p className="m-0 px-1 text-[12px] text-danger">Candidates could not be loaded.</p>
        )}

        {!loading && !error && candidates.length === 0 && (
          <div className="flex flex-col gap-2.5 border border-line bg-panel p-3">
            <p className="m-0 text-[12px] leading-snug text-mist">
              No search has been run for this field.
            </p>
            <Button size="sm" onClick={onRunSearch} disabled={!canSearch}>
              Run moving-object search
            </Button>
            {!canSearch && (
              <p className="m-0 text-[11px] text-haze">
                Wait for the field to finish loading before searching.
              </p>
            )}
          </div>
        )}

        {!loading && !error && candidates.length > 0 && ordered.length === 0 && (
          <div className="flex flex-col gap-2 border border-line bg-panel p-3">
            <p className="m-0 text-[12px] leading-snug text-mist">No candidates match these filters.</p>
            <Button variant="outline" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          </div>
        )}

        {!loading && !error && ordered.length > 0 && (
          <ol className="m-0 flex list-none flex-col gap-1 p-0" aria-label="Ranked candidates">
            {ordered.map((candidate, index) => {
              const selected = candidate.candidate_id === selectedId;
              const hovered = candidate.candidate_id === hoveredId;
              const rate = Math.hypot(
                candidate.motion.mu_e_arcsec_per_day,
                candidate.motion.mu_n_arcsec_per_day,
              );
              return (
                <li key={candidate.candidate_id}>
                  <button
                    type="button"
                    onClick={() => onSelect(candidate.candidate_id)}
                    onMouseEnter={() => onHover(candidate.candidate_id)}
                    onMouseLeave={() => onHover(null)}
                    aria-pressed={selected}
                    className={cn(
                      'flex w-full cursor-pointer items-center gap-2 border px-2 py-1.5 text-left text-inherit transition-colors',
                      selected
                        ? 'border-accent/60 bg-accent-soft'
                        : hovered
                          ? 'border-line-2 bg-panel-2'
                          : 'border-transparent bg-transparent hover:border-line hover:bg-panel',
                    )}
                  >
                    <span className="w-4 shrink-0 font-mono text-[10px] text-haze">
                      {sortBy === 'rank' && filter !== 'top'
                        ? index + 1
                        : (candidate.ordinal ?? index + 1)}
                    </span>
                    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span className="font-mono text-[13px] font-semibold whitespace-nowrap text-ink">
                          {formatId(candidate.ordinal)}
                        </span>
                        <span
                          className={cn(
                            'truncate text-[10px]',
                            candidate.static_source ? 'text-haze' : 'text-accent',
                          )}
                        >
                          {candidate.static_source ? 'Static flag' : 'Moving'}
                        </span>
                      </span>
                      <span className="truncate font-mono text-[10px] text-haze">
                        {formatRate(rate)}
                        {basis === 'stack_snr' && ` · SNR ${candidate.stack_snr.toFixed(1)}`}
                      </span>
                    </span>
                    <span className="shrink-0 text-right font-mono text-[12px] font-medium text-ink">
                      {basis === 'model_score' && candidate.model_score
                        ? candidate.model_score.value.toFixed(2)
                        : candidate.stack_snr.toFixed(1)}
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        )}

        <p className="m-0 mt-auto px-1 text-[10px] leading-snug text-haze">
          Stack SNR is the signal-to-noise of the combined image at this position along this path. It
          is not a detection claim.
        </p>
      </div>

      <footer className="flex shrink-0 flex-col gap-2 border-t border-line px-3 py-2.5">
        {showSkyControls && (
          <>
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] font-semibold tracking-[0.14em] text-haze uppercase">
                Sky layers
              </span>
              <label className="flex items-center gap-1.5 text-[10px] text-haze">
                Grid
                <Switch
                  checked={showGrid}
                  onCheckedChange={onShowGrid}
                  aria-label="Show sky grid"
                  className="scale-90"
                />
              </label>
            </div>
            <div className="grid grid-cols-2 gap-x-2 gap-y-1">
              {LAYERS.map((layer) => (
                <label
                  key={layer.id}
                  className="flex min-w-0 items-center justify-between gap-1 text-[10px] text-mist"
                >
                  <span className="truncate">{layer.label}</span>
                  <Switch
                    checked={layers.includes(layer.id)}
                    onCheckedChange={() => onToggleLayer(layer.id)}
                    aria-label={`Show ${layer.label.toLowerCase()} layer`}
                    className="scale-75"
                  />
                </label>
              ))}
            </div>
          </>
        )}
        <Button
          variant="outline"
          size="sm"
          className="w-full whitespace-nowrap"
          onClick={onExport}
          disabled={!canExport}
        >
          <Download className="size-3.5" aria-hidden="true" />
          Export Candidate Dossier CSV
        </Button>
      </footer>
    </section>
  );
}
