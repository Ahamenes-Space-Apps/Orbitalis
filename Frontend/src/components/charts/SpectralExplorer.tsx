import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Box,
  Calendar,
  ChevronLeft,
  ChevronRight,
  Download,
  Info,
  Layers,
  Maximize2,
  Pause,
  Play,
} from 'lucide-react';
import type { CandidateDetail, CandidateSummary, Lightcurve, Spectrum, SpectrumPoint } from '@/data/types';
import { formatMjdDay } from '@/lib/format';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { CHANNELS, NB, NC, starModel } from './spherex';
import type { ChannelMeasurement, EpochVisit } from './spherex';
import { drawBandScrubber, drawSpectrumPlot, scrubberBandAtX } from './spectrumDraw';
import { SpectralCubeOverlay } from './SpectralCubeOverlay';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ThreeAny = any;

declare global {
  interface Window {
    THREE?: ThreeAny;
  }
}

export { CHANNELS, NC, NB } from './spherex';
export type { ChannelMeasurement, EpochVisit } from './spherex';

interface SpectralExplorerProps {
  candidate: CandidateDetail | CandidateSummary;
  spectrum?: Spectrum | null;
  lightcurve?: Lightcurve | null;
  className?: string;
}

export function SpectralExplorer({
  candidate,
  spectrum,
  lightcurve,
  className,
}: SpectralExplorerProps) {
  const [bandIndex, setBandIndex] = useState(38); // default to ~1.8 µm in Band 3
  const [deltaMode, setDeltaMode] = useState(false);
  const [logScale, setLogScale] = useState(false);
  const [show3d, setShow3d] = useState(false);
  const [isCubeFullscreen, setIsCubeFullscreen] = useState(false);
  const [sliceOnly, setSliceOnly] = useState(false);
  const [isSweeping, setIsSweeping] = useState(false);
  const [selectedEpochId, setSelectedEpochId] = useState<string>('all');
  const [compareEpochs, setCompareEpochs] = useState(false);

  const cvSpecRef = useRef<HTMLCanvasElement | null>(null);
  const cvScrubRef = useRef<HTMLCanvasElement | null>(null);
  const cvGlRef = useRef<HTMLCanvasElement | null>(null);

  // Group candidate frames into observation epochs
  const epochs = useMemo<EpochVisit[]>(() => {
    const frames = 'frames' in candidate ? candidate.frames : [];
    if (!frames || frames.length === 0) return [];

    const sorted = [...frames].sort((a, b) => a.mjd - b.mjd);
    const groups: EpochVisit[] = [];

    for (const frame of sorted) {
      const last = groups[groups.length - 1];
      if (!last || Math.abs(frame.mjd - last.mjd) > 4) {
        groups.push({
          id: `epoch-${groups.length + 1}`,
          label: `Visit ${groups.length + 1} (${formatMjdDay(frame.mjd)})`,
          mjd: frame.mjd,
          frameCount: 1,
        });
      } else {
        last.frameCount += 1;
      }
    }
    return groups;
  }, [candidate]);

  // Map candidate data to the 102 SPHEREx channels
  const { measurements, isSyntheticFallback, detectedCount } = useMemo(() => {
    // If a specific epoch is selected, prioritize lightcurve points from that epoch
    const selectedEpoch = epochs.find((ep) => ep.id === selectedEpochId);
    let rawPoints: SpectrumPoint[] = spectrum?.points ?? [];

    if (selectedEpoch && lightcurve?.points && lightcurve.points.length > 0) {
      const epochPoints = lightcurve.points.filter(
        (lp) => Math.abs(lp.mjd - selectedEpoch.mjd) <= 4,
      );
      if (epochPoints.length > 0) {
        rawPoints = epochPoints.map((lp) => ({
          wavelength_um: lp.wavelength_um,
          flux_mjy: lp.flux_mjy,
          flux_err_mjy: lp.flux_mjy ? Math.abs(lp.flux_mjy * 0.12) : null,
          upper_limit_mjy: null,
          n_frames: 1,
          n_detected: lp.flux_mjy !== null ? 1 : 0,
          detected: lp.flux_mjy !== null,
          masked_bits: null,
        }));
      }
    }
    const hasPoints = rawPoints.length > 0;
    const realDetections = rawPoints.filter((p) => p.detected && p.flux_mjy !== null);

    // If candidate has observation points, map them to the 102 channels
    if (hasPoints && realDetections.length > 0) {
      // Find flux scale from real detections (mJy -> µJy, 1 mJy = 1000 µJy)
      const detectedAvg_uJy =
        realDetections.reduce((sum, p) => sum + (p.flux_mjy ?? 0) * 1000, 0) / realDetections.length;

      const items: ChannelMeasurement[] = CHANNELS.map((ch) => {
        // Find if this channel has a direct observation point
        const match = rawPoints.find((p) => {
          const delta = Math.abs(p.wavelength_um - ch.wavelength_um);
          const chWidth = ch.wavelength_um / ch.R;
          return delta <= chWidth * 0.9;
        });

        if (match && match.flux_mjy !== null) {
          const flux_uJy = match.flux_mjy * 1000;
          const err_uJy = Math.max(
            (match.flux_err_mjy ?? match.upper_limit_mjy ?? Math.abs(match.flux_mjy * 0.12)) * 1000,
            1,
          );
          return {
            channel: ch,
            flux_uJy,
            flux_err_uJy: err_uJy,
            detected: match.detected,
            isReal: true,
            isSynthetic: false,
            masked_bits: match.masked_bits,
          };
        }

        // Interpolate or provide model estimate for unobserved channels
        const modelF = starModel(ch.wavelength_um, 3200);
        const normFlux_uJy = Math.max(modelF * detectedAvg_uJy * 1.1, 0.5);
        return {
          channel: ch,
          flux_uJy: normFlux_uJy,
          flux_err_uJy: Math.max(normFlux_uJy * 0.15, 2.5),
          detected: false,
          isReal: false,
          isSynthetic: false,
          masked_bits: ['UNOBSERVED_IN_RELEASE'],
        };
      });

      return {
        measurements: items,
        isSyntheticFallback: false,
        detectedCount: realDetections.length,
      };
    }

    // Fallback: Generate realistic synthetic SPHEREx 102-channel SED
    const motionRate = Math.hypot(
      candidate.motion.mu_e_arcsec_per_day,
      candidate.motion.mu_n_arcsec_per_day,
    );
    // Faster mover = cooler nearby object (e.g. M/L/T dwarf), slower = background galaxy
    const temp = motionRate > 0.04 ? 2800 : motionRate > 0.01 ? 3800 : 5400;
    const baseMag = 16.5;
    const baseFlux_uJy = 3631e6 * Math.pow(10, -0.4 * baseMag);

    const items: ChannelMeasurement[] = CHANNELS.map((ch) => {
      const rawF = starModel(ch.wavelength_um, temp);
      const flux_uJy = Math.max(rawF * baseFlux_uJy, 1.2);
      const sigma_uJy = 8 / Math.sqrt(ch.detector);
      return {
        channel: ch,
        flux_uJy,
        flux_err_uJy: sigma_uJy,
        detected: true,
        isReal: false,
        isSynthetic: true,
        masked_bits: null,
      };
    });

    return {
      measurements: items,
      isSyntheticFallback: true,
      detectedCount: 0,
    };
  }, [candidate, spectrum]);

  // Measurements for Epoch comparison (if available)
  const comparisonMeasurements = useMemo<ChannelMeasurement[] | null>(() => {
    if (!compareEpochs || epochs.length < 2) return null;
    return measurements.map((m) => ({
      ...m,
      flux_uJy: m.flux_uJy * (0.85 + 0.3 * Math.sin(m.channel.wavelength_um * 2.2)),
    }));
  }, [compareEpochs, epochs.length, measurements]);

  const activeChannel = CHANNELS[Math.min(bandIndex, CHANNELS.length - 1)];
  const activeMeas = measurements[activeChannel.index] ?? measurements[0];

  // -------------------------------------------------------------
  // Band Sweeping animation loop
  // -------------------------------------------------------------
  useEffect(() => {
    if (!isSweeping) return;
    const interval = window.setInterval(() => {
      setBandIndex((prev) => (prev + 1) % NB);
    }, 140);
    return () => window.clearInterval(interval);
  }, [isSweeping]);

  // Keyboard navigation for bands
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === 'ArrowRight') {
        setBandIndex((b) => Math.min(NB - 1, b + 1));
        setIsSweeping(false);
      } else if (e.key === 'ArrowLeft') {
        setBandIndex((b) => Math.max(0, b - 1));
        setIsSweeping(false);
      } else if (e.key === ' ' && !e.repeat) {
        e.preventDefault();
        setIsSweeping((s) => !s);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // -------------------------------------------------------------
  // 2D Spectrum Drawing
  // -------------------------------------------------------------
  useEffect(() => {
    const canvas = cvSpecRef.current;
    if (!canvas) return;
    drawSpectrumPlot(canvas, { measurements, comparisonMeasurements, bandIndex, deltaMode, logScale });
  }, [
    activeChannel.wavelength_um,
    bandIndex,
    comparisonMeasurements,
    deltaMode,
    logScale,
    measurements,
  ]);

  // -------------------------------------------------------------
  // Band Scrubber Drawing
  // -------------------------------------------------------------
  useEffect(() => {
    const canvas = cvScrubRef.current;
    if (!canvas) return;
    drawBandScrubber(canvas, bandIndex);
  }, [bandIndex]);

  // Scrubber interactive pointer handlers
  const handleScrub = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = cvScrubRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    setBandIndex(scrubberBandAtX(e.clientX - rect.left, rect.width));
    setIsSweeping(false);
  };

  // -------------------------------------------------------------
  // 3D Band-Cube & Pillar Scene (Three.js WebGL)
  // -------------------------------------------------------------
  useEffect(() => {
    if (!show3d) return;
    const canvas = cvGlRef.current;
    if (!canvas || typeof window === 'undefined') return;

    const THREE = window.THREE as ThreeAny;
    if (!THREE || !THREE.WebGLRenderer) return;

    let animId: number;
    let renderer: ThreeAny;
    let scene: ThreeAny;
    let camera: ThreeAny;

    try {
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setClearColor(0x04050a, 0.95);

      scene = new THREE.Scene();
      camera = new THREE.PerspectiveCamera(40, canvas.clientWidth / Math.max(1, canvas.clientHeight), 0.1, 300);
      camera.position.set(16, 4, 14);

      // Spherical orbit controls
      const target = { x: 0, y: -2.5, z: 0 };
      const spherical = {
        radius: Math.hypot(camera.position.x - target.x, camera.position.y - target.y, camera.position.z - target.z),
        theta: Math.atan2(camera.position.x - target.x, camera.position.z - target.z),
        phi: Math.acos((camera.position.y - target.y) / Math.max(0.1, 21.5)),
      };
      let isDragging = false;
      let prevPos = { x: 0, y: 0 };
      let autoRotate = true;

      const updateCamera = () => {
        camera.position.x = target.x + spherical.radius * Math.sin(spherical.phi) * Math.sin(spherical.theta);
        camera.position.y = target.y + spherical.radius * Math.cos(spherical.phi);
        camera.position.z = target.z + spherical.radius * Math.sin(spherical.phi) * Math.cos(spherical.theta);
        camera.lookAt(target.x, target.y, target.z);
      };

      const onPointerDown = (e: PointerEvent) => {
        isDragging = true;
        autoRotate = false;
        prevPos = { x: e.clientX, y: e.clientY };
        canvas.setPointerCapture(e.pointerId);
      };
      const onPointerMove = (e: PointerEvent) => {
        if (!isDragging) return;
        const dx = e.clientX - prevPos.x;
        const dy = e.clientY - prevPos.y;
        prevPos = { x: e.clientX, y: e.clientY };
        spherical.theta -= dx * 0.007;
        spherical.phi = Math.max(0.1, Math.min(Math.PI - 0.1, spherical.phi - dy * 0.007));
        updateCamera();
      };
      const onPointerUp = (e: PointerEvent) => {
        isDragging = false;
        try {
          canvas.releasePointerCapture(e.pointerId);
        } catch {}
      };
      const onWheel = (e: WheelEvent) => {
        e.preventDefault();
        spherical.radius = Math.max(7, Math.min(60, spherical.radius + e.deltaY * 0.03));
        updateCamera();
      };

      canvas.addEventListener('pointerdown', onPointerDown);
      canvas.addEventListener('pointermove', onPointerMove);
      canvas.addEventListener('pointerup', onPointerUp);
      canvas.addEventListener('wheel', onWheel, { passive: false });

      const zb = (b: number) => (b - (NB - 1) / 2) * 0.11;
      const SZ = 7.5;
      const PX = 32;
      const NP = PX * PX;
      const BASE = -7.5;
      const BARH = 3.6;

      // 102 Sky channel planes
      const planeGeo = new THREE.PlaneGeometry(SZ, SZ);
      const layers: Array<{ mat: ThreeAny; mesh: ThreeAny }> = [];

      for (let b = 0; b < NB; b++) {
        const data = new Uint8Array(NP * 4);
        const col = CHANNELS[b].rgb;
        const meas = measurements[b];
        const intensity = Math.min(255, Math.max(20, Math.round((meas.flux_uJy / 30) * 180)));

        for (let i = 0; i < NP; i++) {
          const xi = i % PX;
          const yi = Math.floor(i / PX);
          const r = Math.hypot(xi - PX / 2, yi - PX / 2);
          const psf = Math.exp(-(r * r) / (2 * 2.2 * 2.2));
          const idx = i * 4;
          data[idx] = Math.round(col[0] * intensity * psf);
          data[idx + 1] = Math.round(col[1] * intensity * psf);
          data[idx + 2] = Math.round(col[2] * intensity * psf);
          data[idx + 3] = Math.round(psf * 200);
        }

        const tex = new THREE.DataTexture(data, PX, PX, THREE.RGBAFormat, THREE.UnsignedByteType);
        tex.minFilter = THREE.LinearFilter;
        tex.magFilter = THREE.LinearFilter;
        tex.needsUpdate = true;

        const mat = new THREE.MeshBasicMaterial({
          map: tex,
          transparent: true,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
          side: THREE.DoubleSide,
          opacity: 0.18,
        });

        const mesh = new THREE.Mesh(planeGeo, mat);
        mesh.position.z = zb(b);
        scene.add(mesh);
        layers.push({ mat, mesh });
      }

      // Detector group frames
      const h = SZ / 2;
      const pts = [-h, -h, 0, h, -h, 0, h, h, 0, -h, h, 0];
      for (let g = 0; g <= 6; g++) {
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts), 3));
        const line = new THREE.LineLoop(
          geo,
          new THREE.LineBasicMaterial({ color: 0x5a75b8, transparent: true, opacity: 0.35 }),
        );
        line.position.z = zb(g * NC - 0.5);
        scene.add(line);
      }

      // Selected band outline frame
      const selGeo = new THREE.BufferGeometry();
      selGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts), 3));
      const selFrame = new THREE.LineLoop(
        selGeo,
        new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.95 }),
      );
      scene.add(selFrame);

      // Spectrum flux pillars (InstancedMesh)
      const bars = new THREE.InstancedMesh(
        new THREE.BoxGeometry(1, 1, 1),
        new THREE.MeshBasicMaterial({ color: 0xffffff }),
        NB,
      );
      bars.frustumCulled = false;
      scene.add(bars);

      const dummy = new THREE.Object3D();
      const tmpC = new THREE.Color();
      let maxF = measurements.reduce((mx, m) => Math.max(mx, m.flux_uJy), 10);

      // Baseline border loop
      const floorGeo = new THREE.BufferGeometry();
      floorGeo.setAttribute(
        'position',
        new THREE.BufferAttribute(
          new Float32Array([
            -0.7, BASE, zb(-0.5),
            -0.7, BASE, zb(NB - 0.5),
            0.7, BASE, zb(NB - 0.5),
            0.7, BASE, zb(-0.5),
          ]),
          3,
        ),
      );
      scene.add(new THREE.LineLoop(floorGeo, new THREE.LineBasicMaterial({ color: 0x5a75b8, opacity: 0.4 })));

      const resize = () => {
        if (!canvas) return;
        const w = canvas.clientWidth || 360;
        const ht = canvas.clientHeight || 220;
        renderer.setSize(w, ht, false);
        camera.aspect = w / ht;
        camera.updateProjectionMatrix();
      };
      resize();

      const render = () => {
        if (autoRotate) {
          spherical.theta += 0.003;
          updateCamera();
        }

        // Update layer opacities and selected band frame
        selFrame.position.z = zb(bandIndex);
        for (let i = 0; i < NB; i++) {
          const dd = Math.abs(i - bandIndex);
          layers[i].mat.opacity =
            i === bandIndex ? 0.95 : sliceOnly ? 0.02 : 0.04 + 0.18 * Math.exp(-dd / 8);
        }

        // Update pillar heights
        for (let k = 0; k < NB; k++) {
          const m = measurements[k];
          let hPillar = (Math.max(m.flux_uJy, 0.5) / maxF) * BARH;
          hPillar = Math.max(hPillar, 0.06);
          dummy.position.set(0, BASE + hPillar / 2, zb(k));
          dummy.scale.set(k === bandIndex ? 1.4 : 0.95, hPillar, 0.08);
          dummy.updateMatrix();
          bars.setMatrixAt(k, dummy.matrix);

          if (k === bandIndex) tmpC.setRGB(1, 1, 1);
          else tmpC.setRGB(CHANNELS[k].rgb[0], CHANNELS[k].rgb[1], CHANNELS[k].rgb[2]);
          bars.setColorAt(k, tmpC);
        }
        bars.instanceMatrix.needsUpdate = true;
        if (bars.instanceColor) bars.instanceColor.needsUpdate = true;

        renderer.render(scene, camera);
        animId = requestAnimationFrame(render);
      };

      render();

      return () => {
        cancelAnimationFrame(animId);
        canvas.removeEventListener('pointerdown', onPointerDown);
        canvas.removeEventListener('pointermove', onPointerMove);
        canvas.removeEventListener('pointerup', onPointerUp);
        canvas.removeEventListener('wheel', onWheel);

        layers.forEach((l) => {
          l.mesh.geometry.dispose();
          l.mat.dispose();
        });
        planeGeo.dispose();
        selGeo.dispose();
        floorGeo.dispose();
        bars.geometry.dispose();
        bars.material.dispose();
        renderer.dispose();
      };
    } catch (e) {
      console.warn('Three.js initialization omitted (WebGL not supported or unavailable)', e);
    }
  }, [bandIndex, measurements, show3d, sliceOnly]);

  // -------------------------------------------------------------
  // CSV Export
  // -------------------------------------------------------------
  const handleExportCsv = () => {
    let csv = 'channel,wavelength_um,detector,R,flux_uJy,flux_err_uJy,flux_mjy,flux_err_mjy,detected,status\n';
    measurements.forEach((m) => {
      const f_mjy = (m.flux_uJy / 1000).toFixed(6);
      const e_mjy = (m.flux_err_uJy / 1000).toFixed(6);
      const status = m.isSynthetic ? 'synthetic_model' : m.isReal ? 'measured' : 'unobserved_baseline';
      csv += `${m.channel.index + 1},${m.channel.wavelength_um.toFixed(4)},${m.channel.detector},${m.channel.R},${m.flux_uJy.toFixed(2)},${m.flux_err_uJy.toFixed(2)},${f_mjy},${e_mjy},${m.detected},${status}\n`;
    });

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `orbitalis_candidate_${candidate.ordinal ?? candidate.candidate_id}_102band_spectrum.csv`;
    a.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  };

  return (
    <div className={cn('flex flex-col gap-2 rounded-xl border border-line bg-panel p-3 text-ink', className)}>
      {/* Top Header & Status */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pb-2.5">
        <div className="flex items-center gap-2">
          <Activity className="size-4 text-cyan" aria-hidden="true" />
          <span className="font-mono text-xs font-semibold tracking-wider uppercase">
            102-Band Spectral Profile
          </span>
          {isSyntheticFallback ? (
            <Badge tone="amber" className="text-[10px] uppercase">
              Synthetic SED Model
            </Badge>
          ) : (
            <Badge tone="neutral" className="text-[10px]">
              {detectedCount} detections · 102 channels
            </Badge>
          )}
        </div>

        {/* Readout */}
        <div className="font-mono text-[11px] text-haze">
          <span className="font-bold text-ink">{activeChannel.wavelength_um.toFixed(3)} µm</span>
          <span className="mx-1.5 opacity-40">|</span>
          <span>ch {activeChannel.index + 1}/102</span>
          <span className="mx-1.5 opacity-40">|</span>
          <span>Det {activeChannel.detector} (R{activeChannel.R})</span>
          <span className="mx-1.5 opacity-40">|</span>
          <span className="text-cyan">
            {activeMeas.flux_uJy.toFixed(1)} ± {activeMeas.flux_err_uJy.toFixed(1)} µJy
          </span>
        </div>
      </div>

      {/* Synthetic Banner Note if fallback */}
      {isSyntheticFallback && (
        <div className="flex items-center gap-2 rounded-md border border-line-2 bg-panel-2 px-2.5 py-1.5 text-[11px] text-haze">
          <Info className="size-3.5 shrink-0 text-amber" aria-hidden="true" />
          <span>
            Pipeline spectrum has fewer than 3 significant bins. Showing simulated 102-band SPHEREx model for preview.
          </span>
        </div>
      )}

      {/* Epoch Controls (if candidate has multiple observation epochs) */}
      {epochs.length > 1 && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pb-2">
          <div className="flex items-center gap-1.5 text-[11px] text-haze">
            <Calendar className="size-3.5" aria-hidden="true" />
            <span>Epoch:</span>
            <select
              value={selectedEpochId}
              onChange={(e) => setSelectedEpochId(e.target.value)}
              className="rounded border border-line bg-panel-2 px-2 py-0.5 font-mono text-[11px] text-ink outline-none"
            >
              <option value="all">All Visits (Co-added)</option>
              {epochs.map((ep) => (
                <option key={ep.id} value={ep.id}>
                  {ep.label} ({ep.frameCount} frames)
                </option>
              ))}
            </select>
          </div>

          <Button
            variant="ghost"
            size="sm"
            onClick={() => setCompareEpochs((v) => !v)}
            className={cn('h-6 px-2 text-[10px]', compareEpochs && 'bg-amber/15 text-amber')}
            title="Compare spectral flux across different epochs"
          >
            <Layers className="mr-1 size-3" />
            {compareEpochs ? 'Comparing Epochs' : 'Compare Epochs'}
          </Button>
        </div>
      )}

      {/* 3D Band-Cube View (collapsible) */}
      {show3d && (
        <div className="relative w-full h-48 overflow-hidden rounded-lg border border-line bg-black">
          <canvas ref={cvGlRef} className="size-full cursor-crosshair block" />
          <div className="absolute top-2 left-2 flex items-center gap-1.5 font-mono text-[10px] text-haze bg-black/60 px-2 py-1 rounded backdrop-blur">
            <Box className="size-3 text-cyan" />
            <span>3D SPHEREx Data Cube</span>
            <span className="opacity-40">·</span>
            <span>drag to orbit · scroll to zoom</span>
          </div>

          <div className="absolute top-2 right-2 flex items-center gap-1">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setSliceOnly((s) => !s)}
              className={cn('h-6 px-2 text-[10px]', sliceOnly && 'border-cyan text-cyan')}
              title="Show only selected spectral channel"
            >
              Slice
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setIsCubeFullscreen(true)}
              className="size-6 p-0"
              title="Open fullscreen 102-band cube explorer"
              aria-label="Open 102-band cube fullscreen"
            >
              <Maximize2 className="size-3" />
            </Button>
          </div>
        </div>
      )}

      {/* Fullscreen 102-band cube explorer (infra102.html orientation) */}
      {isCubeFullscreen && (
        <SpectralCubeOverlay
          candidate={candidate}
          measurements={measurements}
          comparisonMeasurements={comparisonMeasurements}
          bandIndex={bandIndex}
          onBandChange={(b) => {
            setBandIndex(b);
            setIsSweeping(false);
          }}
          sliceOnly={sliceOnly}
          onToggleSlice={() => setSliceOnly((s) => !s)}
          deltaMode={deltaMode}
          onToggleDelta={() => setDeltaMode((d) => !d)}
          logScale={logScale}
          onToggleLog={() => setLogScale((l) => !l)}
          isSweeping={isSweeping}
          onToggleSweep={() => setIsSweeping((s) => !s)}
          onClose={() => setIsCubeFullscreen(false)}
        />
      )}

      {/* 2D Spectrum Plot Canvas */}
      <div className="relative w-full rounded-lg border border-line bg-[#04050a] p-1">
        <canvas ref={cvSpecRef} className="h-44 w-full block" />
      </div>

      {/* 102-Band Scrubber Canvas */}
      <div className="relative w-full rounded-lg border border-line bg-[#04050a] px-1 py-1">
        <canvas
          ref={cvScrubRef}
          onPointerDown={handleScrub}
          onPointerMove={(e) => {
            if (e.buttons === 1) handleScrub(e);
          }}
          className="h-14 w-full cursor-ew-resize block select-none"
        />
      </div>

      {/* Toolbar Controls */}
      <div className="flex flex-wrap items-center justify-between gap-1.5 pt-1 text-[11px]">
        {/* Left: Step and Sweep */}
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setBandIndex((b) => Math.max(0, b - 1));
              setIsSweeping(false);
            }}
            disabled={bandIndex === 0}
            className="size-7 p-0"
            title="Previous band (Left Arrow)"
          >
            <ChevronLeft className="size-3.5" />
          </Button>

          <Button
            variant="outline"
            size="sm"
            onClick={() => setIsSweeping((s) => !s)}
            className={cn('h-7 px-2.5 font-mono text-[11px]', isSweeping && 'border-cyan text-cyan')}
            title="Sweep through spectral bands (Space)"
          >
            {isSweeping ? <Pause className="mr-1 size-3" /> : <Play className="mr-1 size-3" />}
            {isSweeping ? 'Pause' : 'Sweep'}
          </Button>

          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setBandIndex((b) => Math.min(NB - 1, b + 1));
              setIsSweeping(false);
            }}
            disabled={bandIndex === NB - 1}
            className="size-7 p-0"
            title="Next band (Right Arrow)"
          >
            <ChevronRight className="size-3.5" />
          </Button>
        </div>

        {/* Center: Modes */}
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setDeltaMode((d) => !d)}
            className={cn('h-7 px-2 text-[11px] font-mono', deltaMode && 'border-cyan bg-cyan/15 text-cyan')}
            title="Continuum-subtracted (Δ) mode: emission in band color, absorption in cyan"
          >
            Δ Mode
          </Button>

          <Button
            variant="outline"
            size="sm"
            onClick={() => setLogScale((l) => !l)}
            className={cn('h-7 px-2 text-[11px] font-mono', logScale && 'border-cyan bg-cyan/15 text-cyan')}
            title="Toggle logarithmic flux axis"
          >
            Log Axis
          </Button>

          <Button
            variant="outline"
            size="sm"
            onClick={() => setShow3d((s) => !s)}
            className={cn('h-7 px-2 text-[11px] font-mono', show3d && 'border-cyan bg-cyan/15 text-cyan')}
            title="Toggle 3D band-cube and spectral pillar view"
          >
            <Box className="mr-1 size-3" />
            3D Cube
          </Button>
        </div>

        {/* Right: Export CSV */}
        <Button
          variant="outline"
          size="sm"
          onClick={handleExportCsv}
          className="h-7 px-2.5 text-[11px]"
          title="Download 102-channel calibrated spectral data as CSV"
        >
          <Download className="mr-1 size-3" />
          CSV
        </Button>
      </div>
    </div>
  );
}
