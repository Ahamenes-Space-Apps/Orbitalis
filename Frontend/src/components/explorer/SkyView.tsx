import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { Crosshair, Maximize2, Minimize2, Minus, Plus } from 'lucide-react';
import { api } from '@/data/api';
import type { CandidateSummary, Field, Frame, Overlay } from '@/data/types';
import { formatDec, formatGalactic, formatRa, formatWavelength } from '@/lib/format';
import { skyToOffset, toGalactic, type SkyPos } from '@/lib/coords';
import type { CoordinateSystem, LayerName } from '@/state/urlState';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { AladinSky } from './AladinSky';

export interface SkyMarker {
  candidate: CandidateSummary;
  position: SkyPos;
  selected: boolean;
  hovered: boolean;
  staticFlag: boolean;
}

/** One stored per-frame position along a candidate's track (no extrapolation). */
export interface TrackPoint {
  mjd: number;
  ra: number;
  dec: number;
}

const MAX_ZOOM = 24;

interface SkyViewProps {
  field: Field;
  frames: Frame[];
  candidates: CandidateSummary[];
  selectedId: string | null;
  hoveredId: string | null;
  frame: Frame | null;
  view: 'simple' | 'aladin';
  sys: CoordinateSystem;
  layers: LayerName[];
  showFlags: boolean;
  showGrid: boolean;
  resetSignal: number;
  known: Overlay | undefined;
  hypothesis: Overlay | undefined;
  onSelect: (candidateId: string | null) => void;
  onHover: (candidateId: string | null) => void;
  onCursor: (position: SkyPos | null) => void;
  onSkyClick: (position: SkyPos) => void;
  /** Stored per-frame positions of the selected candidate, sorted by MJD. */
  track?: TrackPoint[] | null;
}

const CANDIDATE_COLOR = '#39b8c4';
const STATIC_COLOR = '#82929b';
const KNOWN_COLOR = '#b2bec6';
const HYPOTHESIS_COLOR = '#e76f51';

interface MarkerGroup {
  id: string;
  markers: SkyMarker[];
  pixel: { x: number; y: number };
}

export function SkyView({
  field,
  frames,
  candidates,
  selectedId,
  hoveredId,
  frame,
  view,
  sys,
  layers,
  showFlags,
  showGrid,
  resetSignal,
  known,
  hypothesis,
  onSelect,
  onHover,
  onCursor,
  onSkyClick,
  track = null,
}: SkyViewProps) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [aladinFailed, setAladinFailed] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null);
  const insetRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const sync = () => setIsFullscreen(document.fullscreenElement === rootRef.current);
    document.addEventListener('fullscreenchange', sync);
    return () => document.removeEventListener('fullscreenchange', sync);
  }, []);

  const toggleFullscreen = () => {
    if (document.fullscreenElement === rootRef.current) {
      void document.exitFullscreen();
    } else {
      void rootRef.current?.requestFullscreen?.();
    }
  };

  const grid = field.grid;
  const width = grid.width;
  const height = grid.height;
  const scale = grid.pixel_scale_arcsec;
  const currentFrame = frame ?? frames[0] ?? null;

  const toPixel = useCallback(
    (position: SkyPos): { x: number; y: number } => {
      const offset = skyToOffset({ ra: grid.ra, dec: grid.dec }, position);
      const angle = (-(grid.rotation_deg ?? 0) * Math.PI) / 180;
      const rotatedX = offset.dx * Math.cos(angle) - offset.dy * Math.sin(angle);
      const rotatedY = offset.dx * Math.sin(angle) + offset.dy * Math.cos(angle);
      return {
        x: width / 2 - rotatedX / scale,
        y: height / 2 - rotatedY / scale,
      };
    },
    [grid.dec, grid.ra, grid.rotation_deg, height, scale, width],
  );

  const fromPixel = useCallback(
    (x: number, y: number): SkyPos => {
      const dx = (width / 2 - x) * scale;
      const dy = (height / 2 - y) * scale;
      const angle = ((grid.rotation_deg ?? 0) * Math.PI) / 180;
      const rotatedX = dx * Math.cos(angle) - dy * Math.sin(angle);
      const rotatedY = dx * Math.sin(angle) + dy * Math.cos(angle);
      return {
        ra: grid.ra + rotatedX / 3600 / Math.cos((grid.dec * Math.PI) / 180),
        dec: grid.dec + rotatedY / 3600,
      };
    },
    [grid.dec, grid.ra, grid.rotation_deg, height, scale, width],
  );

  const markers = useMemo<SkyMarker[]>(() => {
    return candidates.map((candidate) => {
      return {
        candidate,
        // Candidate markers are always anchored to the stored reference position.
        // Movement is shown only by the selected candidate's stored per-frame track.
        position: candidate.position,
        selected: candidate.candidate_id === selectedId,
        hovered: candidate.candidate_id === hoveredId,
        staticFlag: Boolean(candidate.static_source),
      };
    });
  }, [candidates, hoveredId, selectedId]);

  const selectedMarker = markers.find((marker) => marker.selected) ?? null;
  const [expandedGroupId, setExpandedGroupId] = useState<string | null>(null);

  const markerGroups = useMemo<MarkerGroup[]>(() => {
    const clampMarkerPixel = (point: { x: number; y: number }) => ({
      x: Math.min(width - 0.2, Math.max(0.2, point.x)),
      y: Math.min(height - 0.2, Math.max(0.2, point.y)),
    });
    const groups: MarkerGroup[] = [];
    for (const marker of markers) {
      const rawPixel = toPixel(marker.position);
      const pixel = clampMarkerPixel(rawPixel);
      const group = groups.find((candidateGroup) => {
        return Math.hypot(candidateGroup.pixel.x - pixel.x, candidateGroup.pixel.y - pixel.y) <= 3.2;
      });
      if (!group) {
        groups.push({
          id: marker.candidate.candidate_id,
          markers: [marker],
          pixel,
        });
        continue;
      }
      group.markers.push(marker);
      group.pixel = {
        x: group.markers.reduce((sum, item) => sum + clampMarkerPixel(toPixel(item.position)).x, 0) / group.markers.length,
        y: group.markers.reduce((sum, item) => sum + clampMarkerPixel(toPixel(item.position)).y, 0) / group.markers.length,
      };
      group.id = group.markers.map((item) => item.candidate.candidate_id).sort().join(':');
    }
    return groups;
  }, [height, markers, toPixel, width]);

  const trackPixels = useMemo(() => {
    if (!track || track.length === 0) return [];
    return [...track]
      .sort((a, b) => a.mjd - b.mjd)
      .map((point) => {
        const pixel = toPixel({ ra: point.ra, dec: point.dec });
        return { ...point, x: pixel.x, y: pixel.y };
      });
  }, [toPixel, track]);

  const currentMjd = currentFrame?.mjd ?? null;

  const trackPath = useMemo(() => {
    if (trackPixels.length < 2) return null;
    return trackPixels
      .map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
      .join(' ');
  }, [trackPixels]);

  /** Total measured displacement across the stored positions, in arcsec and pixels. */
  const trackMotion = useMemo(() => {
    if (trackPixels.length < 2) return null;
    const first = trackPixels[0];
    const last = trackPixels[trackPixels.length - 1];
    const meanDec = ((first.dec + last.dec) / 2) * (Math.PI / 180);
    let dRaDeg = last.ra - first.ra;
    dRaDeg = ((((dRaDeg + 180) % 360) + 360) % 360) - 180;
    const arcsec = Math.hypot(dRaDeg * Math.cos(meanDec) * 3600, (last.dec - first.dec) * 3600);
    return { arcsec, px: arcsec / Math.max(grid.pixel_scale_arcsec, 1e-6) };
  }, [grid.pixel_scale_arcsec, trackPixels]);

  const panForCenter = useCallback(
    (fx: number, fy: number, viewW: number, viewH: number) => {
      const maxX = Math.max(0, width - viewW);
      const maxY = Math.max(0, height - viewH);
      const tx = Math.min(maxX, Math.max(0, fx - viewW / 2));
      const ty = Math.min(maxY, Math.max(0, fy - viewH / 2));
      setPan({ x: tx - (width - viewW) / 2, y: ty - (height - viewH) / 2 });
    },
    [height, width],
  );

  const fitViewToTrack = useCallback(() => {
    if (trackPixels.length === 0) {
      setZoom(1);
      setPan({ x: 0, y: 0 });
      return;
    }
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const point of trackPixels) {
      minX = Math.min(minX, point.x);
      minY = Math.min(minY, point.y);
      maxX = Math.max(maxX, point.x);
      maxY = Math.max(maxY, point.y);
    }
    const spanX = Math.max(maxX - minX, 4);
    const spanY = Math.max(maxY - minY, 4);
    const requiredW = spanX * 1.6 + 10;
    const requiredH = spanY * 1.6 + 10;
    const next = Math.min(
      MAX_ZOOM,
      Math.max(1, Math.min(width / requiredW, height / requiredH)),
    );
    const viewW = width / next;
    const viewH = height / next;
    panForCenter((minX + maxX) / 2, (minY + maxY) / 2, viewW, viewH);
    setZoom(next);
  }, [height, panForCenter, trackPixels, width]);

  const fittedTrack = useRef<string | null>(null);
  useEffect(() => {
    if (!selectedId) {
      fittedTrack.current = null;
      return;
    }
    if (trackPixels.length === 0) return;
    const key = `${selectedId}:${trackPixels.length}`;
    if (fittedTrack.current === key) return;
    fittedTrack.current = key;
    fitViewToTrack();
  }, [fitViewToTrack, selectedId, trackPixels.length]);

  const viewBox = useMemo(() => {
    const viewWidth = width / zoom;
    const viewHeight = height / zoom;
    const maxX = Math.max(0, width - viewWidth);
    const maxY = Math.max(0, height - viewHeight);
    const x = Math.min(maxX, Math.max(0, (width - viewWidth) / 2 + pan.x));
    const y = Math.min(maxY, Math.max(0, (height - viewHeight) / 2 + pan.y));
    return { x, y, width: viewWidth, height: viewHeight };
  }, [height, pan.x, pan.y, width, zoom]);

  // Keep stored marker positions inside the field image when the WCS lands a
  // point just outside the crop.
  const clampPixel = useCallback(
    (point: { x: number; y: number }, margin: number) => ({
      x: Math.min(width - margin, Math.max(margin, point.x)),
      y: Math.min(height - margin, Math.max(margin, point.y)),
    }),
    [width, height],
  );

  const resetView = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  };

  useEffect(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [resetSignal]);

  const centerOnSelection = () => {
    if (!selectedMarker) {
      resetView();
      return;
    }
    if (trackPixels.length > 0) {
      fitViewToTrack();
      return;
    }
    const pixel = toPixel(selectedMarker.position);
    panForCenter(pixel.x, pixel.y, viewBox.width, viewBox.height);
  };

  const onInsetClick = (event: React.MouseEvent<SVGSVGElement>) => {
    const svg = insetRef.current;
    const ctm = svg?.getScreenCTM();
    if (!svg || !ctm) return;
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(ctm.inverse());
    const fx = Math.min(width, Math.max(0, point.x));
    const fy = Math.min(height, Math.max(0, point.y));
    panForCenter(fx, fy, viewBox.width, viewBox.height);
  };

  const onPointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (zoom === 1) return;
    drag.current = { x: event.clientX, y: event.clientY, panX: pan.x, panY: pan.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const pixelX = viewBox.x + ((event.clientX - bounds.left) / bounds.width) * viewBox.width;
    const pixelY = viewBox.y + ((event.clientY - bounds.top) / bounds.height) * viewBox.height;
    onCursor(fromPixel(pixelX, pixelY));
    if (drag.current) {
      const scaleX = viewBox.width / bounds.width;
      const scaleY = viewBox.height / bounds.height;
      setPan({
        x: drag.current.panX + (drag.current.x - event.clientX) * scaleX,
        y: drag.current.panY + (drag.current.y - event.clientY) * scaleY,
      });
    }
  };

  const endDrag = () => {
    drag.current = null;
  };

  const raTicks = [0.25, 0.5, 0.75].map((fraction) => {
    const x = viewBox.x + fraction * viewBox.width;
    const position = fromPixel(x, viewBox.y);
    return { x, label: sys === 'eq' ? formatRa(position.ra) : formatGalactic(position).split('·')[0] };
  });

  const decTicks = [0.1, 0.5, 0.9].map((fraction) => {
    const y = viewBox.y + fraction * viewBox.height;
    const position = fromPixel(viewBox.x, y);
    return { y, label: sys === 'eq' ? formatDec(position.dec) : `b ${toGalactic(position).b.toFixed(2)}°` };
  });

  const gridLines = useMemo(() => {
    const step = Math.max(2, Math.round(width / 10));
    const vertical = [];
    const horizontal = [];
    for (let x = step; x < width; x += step) vertical.push(x);
    for (let y = step; y < height; y += step) horizontal.push(y);
    return { vertical, horizontal };
  }, [height, width]);

  const imageUrl = currentFrame ? api.frameImage(field.field_id, currentFrame.frame_ref) : null;
  const flagsUrl =
    currentFrame && showFlags ? api.flagsImage(field.field_id, currentFrame.frame_ref) : null;

  const hypothesisShapes = (hypothesis?.items ?? []).filter(
    (item) => item.shape && item.contains_field,
  );

  const insetPadX = width * 0.45;
  const insetPadY = height * 0.45;
  const showInset = view !== 'aladin' || aladinFailed !== null;

  return (
    <div
      ref={rootRef}
      className={cn('absolute inset-0 overflow-hidden bg-[#0a0f1c]', isFullscreen && 'fixed z-50')}
    >
      {view === 'aladin' && !aladinFailed ? (
        <AladinSky
          field={field}
          candidates={candidates}
          selectedId={selectedId}
          onSelect={onSelect}
          onFailed={setAladinFailed}
        />
      ) : (
        <svg
          role="img"
          aria-label={`Sky field at ${formatRa(grid.ra)}, ${formatDec(grid.dec)}. ${
            candidates.length
          } unconfirmed candidates.`}
          viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.width} ${viewBox.height}`}
          className="h-full w-full touch-none"
          tabIndex={0}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerLeave={() => {
            endDrag();
            onCursor(null);
          }}
          onWheel={(event) => {
            event.preventDefault();
            setZoom((value) => Math.min(MAX_ZOOM, Math.max(1, value * (event.deltaY < 0 ? 1.2 : 0.84))));
          }}
          onClick={(event) => {
            if (drag.current) return;
            const bounds = event.currentTarget.getBoundingClientRect();
            const pixelX = viewBox.x + ((event.clientX - bounds.left) / bounds.width) * viewBox.width;
            const pixelY = viewBox.y + ((event.clientY - bounds.top) / bounds.height) * viewBox.height;
            onSkyClick(fromPixel(pixelX, pixelY));
          }}
          onKeyDown={(event) => {
            if (event.key === '+' || event.key === '=') setZoom((value) => Math.min(MAX_ZOOM, value * 1.3));
            if (event.key === '-' || event.key === '_') setZoom((value) => Math.max(1, value / 1.3));
            if (event.key === 'Escape') onSelect(null);
          }}
        >
          <defs>
            <clipPath id="field-clip">
              <rect x="0" y="0" width={width} height={height} />
            </clipPath>
          </defs>

          <rect x="0" y="0" width={width} height={height} fill="#0d1319" />
          {imageUrl && (
            <image
              href={imageUrl}
              x="0"
              y="0"
              width={width}
              height={height}
              preserveAspectRatio="none"
              opacity="0.92"
            />
          )}
          {flagsUrl && (
            <image
              href={flagsUrl}
              x="0"
              y="0"
              width={width}
              height={height}
              preserveAspectRatio="none"
              opacity="0.72"
            />
          )}

          <g clipPath="url(#field-clip)" aria-hidden="true">
            {showGrid &&
              gridLines.vertical.map((x) => (
                <line key={`v${x}`} x1={x} x2={x} y1="0" y2={height} className="sky-grid" strokeWidth="0.06" />
              ))}
            {showGrid &&
              gridLines.horizontal.map((y) => (
                <line key={`h${y}`} x1="0" x2={width} y1={y} y2={y} className="sky-grid" strokeWidth="0.06" />
              ))}
            <rect x="0" y="0" width={width} height={height} fill="none" stroke="#364752" strokeWidth="0.12" />
          </g>

          {layers.includes('hypothesis') &&
            hypothesisShapes.map((item) => {
              const shape = item.shape;
              if (!shape) return null;
              if (shape.type === 'box') {
                const a = toPixel({ ra: shape.ra_min, dec: shape.dec_max });
                const b = toPixel({ ra: shape.ra_max, dec: shape.dec_min });
                return (
                  <rect
                    key={item.id}
                    x={Math.min(a.x, b.x)}
                    y={Math.min(a.y, b.y)}
                    width={Math.abs(a.x - b.x)}
                    height={Math.abs(a.y - b.y)}
                    fill={HYPOTHESIS_COLOR}
                    fillOpacity="0.07"
                    stroke={HYPOTHESIS_COLOR}
                    strokeDasharray="0.7 0.5"
                    strokeWidth="0.12"
                  >
                    <title>{item.name}</title>
                  </rect>
                );
              }
              const center = toPixel({ ra: shape.ra, dec: shape.dec });
              return (
                <circle
                  key={item.id}
                  cx={center.x}
                  cy={center.y}
                  r={(shape.radius_deg * 3600) / scale}
                  fill={HYPOTHESIS_COLOR}
                  fillOpacity="0.07"
                  stroke={HYPOTHESIS_COLOR}
                  strokeDasharray="0.7 0.5"
                  strokeWidth="0.12"
                >
                  <title>{item.name}</title>
                </circle>
              );
            })}

          {layers.includes('known') &&
            (known?.items ?? []).map((item) => {
              if (item.ra === null || item.dec === null) return null;
              const pixel = toPixel({ ra: item.ra, dec: item.dec });
              return (
                <g key={item.id}>
                  <rect
                    x={pixel.x - 0.7}
                    y={pixel.y - 0.7}
                    width="1.4"
                    height="1.4"
                    transform={`rotate(45 ${pixel.x} ${pixel.y})`}
                    fill="none"
                    stroke={KNOWN_COLOR}
                    strokeWidth="0.16"
                  />
                  <title>{item.name}</title>
                </g>
              );
            })}

          {layers.includes('trails') && trackPixels.length > 0 && selectedMarker && (
            <g aria-hidden="true">
              {trackPath && (
                <path
                  d={trackPath}
                  fill="none"
                  stroke={CANDIDATE_COLOR}
                  strokeOpacity="0.55"
                  strokeWidth="0.18"
                />
              )}
              {trackPixels.map((point, index) => {
                const observed = currentMjd === null || point.mjd <= currentMjd + 1e-6;
                return (
                  <circle
                    key={`stored-${index}`}
                    cx={point.x}
                    cy={point.y}
                    r={observed ? 0.5 : 0.38}
                    fill={observed ? CANDIDATE_COLOR : 'none'}
                    stroke={CANDIDATE_COLOR}
                    strokeOpacity={observed ? 0.95 : 0.4}
                    strokeWidth="0.14"
                  />
                );
              })}
            </g>
          )}

          {markerGroups.map((group) => {
            const visibleMarkers = group.markers.filter((marker) =>
              marker.staticFlag ? layers.includes('static') : layers.includes('moving'),
            );
            if (visibleMarkers.length === 0) return null;
            const expanded =
              expandedGroupId === group.id || visibleMarkers.some((marker) => marker.selected);
            if (visibleMarkers.length > 1 && !expanded) {
              return (
                <g
                  key={group.id}
                  className="cursor-pointer"
                  role="button"
                  tabIndex={0}
                  aria-label={`${visibleMarkers.length} overlapping unconfirmed candidates. Expand markers.`}
                  onClick={(event) => {
                    event.stopPropagation();
                    setExpandedGroupId(group.id);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setExpandedGroupId(group.id);
                    }
                  }}
                >
                  <circle cx={group.pixel.x} cy={group.pixel.y} r="2.7" fill="#101820" stroke={CANDIDATE_COLOR} strokeWidth="0.22" />
                  <text
                    x={group.pixel.x}
                    y={group.pixel.y + 0.45}
                    textAnchor="middle"
                    fill={CANDIDATE_COLOR}
                    fontSize="1.5"
                    fontFamily="'IBM Plex Mono', monospace"
                  >
                    {visibleMarkers.length}
                  </text>
                  <title>{`${visibleMarkers.length} overlapping unconfirmed candidates. Click to expand.`}</title>
                </g>
              );
            }
            return visibleMarkers.map((marker) => {
              const color = marker.staticFlag ? STATIC_COLOR : CANDIDATE_COLOR;
              const half = marker.selected ? 1.9 : marker.hovered ? 1.7 : 1.45;
              const pixel = clampPixel(toPixel(marker.position), half + 0.1);
              return (
                <g
                  key={marker.candidate.candidate_id}
                  className="cursor-pointer"
                  onClick={(event) => {
                    event.stopPropagation();
                    setExpandedGroupId(null);
                    onSelect(marker.candidate.candidate_id);
                  }}
                  onMouseEnter={() => onHover(marker.candidate.candidate_id)}
                  onMouseLeave={() => onHover(null)}
                  role="button"
                  aria-label={`${marker.candidate.ordinal ? `#${marker.candidate.ordinal}` : 'Candidate'}, unconfirmed`}
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setExpandedGroupId(null);
                      onSelect(marker.candidate.candidate_id);
                    }
                  }}
                >
                  {marker.selected && (
                    <rect
                      x={pixel.x - half * 2.2}
                      y={pixel.y - half * 2.2}
                      width={half * 4.4}
                      height={half * 4.4}
                      rx={0.9}
                      fill={CANDIDATE_COLOR}
                      opacity="0.13"
                    />
                  )}
                  <rect
                    x={pixel.x - half}
                    y={pixel.y - half}
                    width={half * 2}
                    height={half * 2}
                    fill="none"
                    stroke={color}
                    strokeWidth={marker.selected ? 0.24 : 0.17}
                    strokeDasharray={marker.staticFlag ? '0.45 0.3' : undefined}
                  />
                  {expanded && visibleMarkers.length > 1 ? null : (
                    <text
                      x={pixel.x + half + 0.4}
                      y={pixel.y + 0.35}
                      fill={color}
                      fontSize="1.05"
                      fontFamily="'IBM Plex Mono', monospace"
                      paintOrder="stroke"
                      stroke="#0d1319"
                      strokeWidth="0.22"
                    >
                      {marker.candidate.ordinal ? `#${String(marker.candidate.ordinal).padStart(3, '0')}` : '#'}
                    </text>
                  )}
                  <title>
                    {`${marker.candidate.ordinal ? `#${marker.candidate.ordinal}` : 'Candidate'} · ${
                      marker.staticFlag ? 'static-source flag' : 'moving'
                    } · stack SNR ${marker.candidate.stack_snr.toFixed(1)} · unconfirmed`}
                  </title>
                </g>
              );
            });
          })}

          {raTicks.map((tick, index) => (
            <text
              key={`ra-${tick.x}`}
              x={tick.x}
              y={viewBox.y + 2.2}
              textAnchor={
                index === 0 ? 'start' : index === raTicks.length - 1 ? 'end' : 'middle'
              }
              fill="#82929b"
              fontSize="0.95"
              fontFamily="'IBM Plex Mono', monospace"
            >
              {tick.label}
            </text>
          ))}
          {decTicks.map((tick) => (
            <text
              key={`dec-${tick.y}`}
              x={viewBox.x + 0.4}
              y={tick.y}
              textAnchor="start"
              fill="#82929b"
              fontSize="0.95"
              fontFamily="'IBM Plex Mono', monospace"
            >
              {tick.label}
            </text>
          ))}
        </svg>
      )}

      {showInset && (
        <div className="absolute bottom-3 left-3 z-10 flex flex-col gap-1">
          <svg
            ref={insetRef}
            viewBox={`${-insetPadX} ${-insetPadY} ${width + insetPadX * 2} ${
              height + insetPadY * 2
            }`}
            preserveAspectRatio="xMidYMid meet"
            role="img"
            aria-label="Wide-sky context around the field. Click to recenter the view."
            onClick={onInsetClick}
            className="block h-[92px] w-[124px] cursor-crosshair rounded-sm border border-sky-line/60 bg-[#0d1319]/90"
          >
            <title>Wide-sky inset: the field sits inside a wider patch. Click to recenter.</title>
            <rect
              x={-insetPadX}
              y={-insetPadY}
              width={width + insetPadX * 2}
              height={height + insetPadY * 2}
              fill="#0d1319"
            />
            <rect x="0" y="0" width={width} height={height} fill="none" stroke="#364752" strokeWidth="0.6" />
            {trackPath && (
              <path d={trackPath} fill="none" stroke={CANDIDATE_COLOR} strokeOpacity="0.6" strokeWidth="0.9" />
            )}
            {trackPixels.map((point, index) => (
              <circle
                key={`inset-track-${index}`}
                cx={point.x}
                cy={point.y}
                r="1.3"
                fill={CANDIDATE_COLOR}
                fillOpacity="0.85"
              />
            ))}
            {markerGroups.map((group) => {
              const visibleMarkers = group.markers.filter((marker) =>
                marker.staticFlag ? layers.includes('static') : layers.includes('moving'),
              );
              if (visibleMarkers.length === 0) return null;
              return visibleMarkers.length > 1 ? (
                <g key={`inset-${group.id}`}>
                  <circle cx={group.pixel.x} cy={group.pixel.y} r="2.6" fill="#101820" stroke={CANDIDATE_COLOR} strokeWidth="0.6" />
                  <text x={group.pixel.x} y={group.pixel.y + 0.9} textAnchor="middle" fill={CANDIDATE_COLOR} fontSize="2.2" fontFamily="'IBM Plex Mono', monospace">
                    {visibleMarkers.length}
                  </text>
                </g>
              ) : (
                <circle
                  key={`inset-${group.id}`}
                  cx={group.pixel.x}
                  cy={group.pixel.y}
                  r={visibleMarkers[0].selected ? 2 : 1.1}
                  fill={visibleMarkers[0].staticFlag ? STATIC_COLOR : CANDIDATE_COLOR}
                  fillOpacity={visibleMarkers[0].selected ? 1 : 0.7}
                />
              );
            })}
            <rect
              x={viewBox.x}
              y={viewBox.y}
              width={viewBox.width}
              height={viewBox.height}
              fill="rgba(123,147,255,0.10)"
              stroke="#39b8c4"
              strokeWidth="0.8"
            />
          </svg>
          <span className="w-[124px] text-center text-[10px] leading-tight text-star/70">
            Patch context · click to recenter
          </span>
        </div>
      )}

      <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-end p-3">
        <div className="pointer-events-auto flex flex-col items-end gap-1.5">
          <div className="flex items-center gap-0.5 rounded-sm border border-sky-line/70 bg-sky/90 p-0.5">
            <Button
              variant="ghost"
              size="icon"
              className="size-7 min-h-0 text-star hover:bg-white/10 hover:text-star"
              aria-label="Zoom in"
              onClick={() => setZoom((v) => Math.min(MAX_ZOOM, v * 1.3))}
            >
              <Plus className="size-3.5" aria-hidden="true" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="size-7 min-h-0 text-star hover:bg-white/10 hover:text-star"
              aria-label="Zoom out"
              onClick={() => setZoom((v) => Math.max(1, v / 1.3))}
            >
              <Minus className="size-3.5" aria-hidden="true" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="size-7 min-h-0 text-star hover:bg-white/10 hover:text-star"
              aria-label="Recentre on selection or field"
              onClick={centerOnSelection}
            >
              <Crosshair className="size-3.5" aria-hidden="true" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="size-7 min-h-0 text-star hover:bg-white/10 hover:text-star"
              aria-label={isFullscreen ? 'Exit fullscreen sky view' : 'View sky fullscreen'}
              aria-pressed={isFullscreen}
              onClick={toggleFullscreen}
            >
              {isFullscreen ? (
                <Minimize2 className="size-3.5" aria-hidden="true" />
              ) : (
                <Maximize2 className="size-3.5" aria-hidden="true" />
              )}
            </Button>
          </div>
          <span className="rounded-sm border border-sky-line/70 bg-sky/90 px-2 py-1 font-mono text-[10px] text-star/80">
            {currentFrame
              ? `${formatWavelength(currentFrame.wavelength_um)} · D${currentFrame.detector}`
              : 'No frame'}
          </span>
          {trackMotion && (
            <span
              className="rounded-sm border border-sky-line/70 bg-sky/90 px-2 py-1 font-mono text-[10px] text-star/80"
              title="Total displacement across the stored per-frame positions (first to last MJD)"
            >
              Track Δ {trackMotion.arcsec.toFixed(2)}″ · {trackMotion.px.toFixed(1)} px
            </span>
          )}
          {aladinFailed && (
            <span className="max-w-[260px] rounded-sm border border-accent/40 bg-sky/90 px-2.5 py-1 text-[11px] text-accent">
              Aladin could not start; showing the offline view.
            </span>
          )}
          {!currentFrame && (
            <span className="rounded-sm border border-sky-line/70 bg-sky/90 px-2.5 py-1 text-[11px] text-star/70">
              Waiting for the first frame
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
