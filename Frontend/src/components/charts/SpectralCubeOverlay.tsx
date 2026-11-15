import { useEffect, useRef } from 'react';
import { Box, Pause, Play, X } from 'lucide-react';
import type { CandidateDetail, CandidateSummary } from '@/data/types';
import { CHANNELS, NB, NC } from './spherex';
import type { ChannelMeasurement } from './spherex';
import { drawBandScrubber, drawSpectrumPlot, scrubberBandAtX } from './spectrumDraw';
import { cn } from '@/lib/utils';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ThreeAny = any;

declare global {
  interface Window {
    THREE?: ThreeAny;
  }
}

interface SpectralCubeOverlayProps {
  candidate: CandidateDetail | CandidateSummary;
  measurements: ChannelMeasurement[];
  comparisonMeasurements: ChannelMeasurement[] | null;
  bandIndex: number;
  onBandChange: (band: number) => void;
  sliceOnly: boolean;
  onToggleSlice: () => void;
  deltaMode: boolean;
  onToggleDelta: () => void;
  logScale: boolean;
  onToggleLog: () => void;
  isSweeping: boolean;
  onToggleSweep: () => void;
  onClose: () => void;
}

const PANEL =
  'absolute rounded-lg border border-[rgba(140,170,255,0.16)] bg-[rgba(8,12,26,0.64)] backdrop-blur-md';

export function SpectralCubeOverlay({
  candidate,
  measurements,
  comparisonMeasurements,
  bandIndex,
  onBandChange,
  sliceOnly,
  onToggleSlice,
  deltaMode,
  onToggleDelta,
  logScale,
  onToggleLog,
  isSweeping,
  onToggleSweep,
  onClose,
}: SpectralCubeOverlayProps) {
  const glRef = useRef<HTMLCanvasElement | null>(null);
  const specRef = useRef<HTMLCanvasElement | null>(null);
  const scrubRef = useRef<HTMLCanvasElement | null>(null);

  // Live refs so the WebGL loop reads fresh state without re-creating the scene
  const live = useRef({ bandIndex, sliceOnly, measurements });
  live.current = { bandIndex, sliceOnly, measurements };

  const activeChannel = CHANNELS[Math.min(bandIndex, CHANNELS.length - 1)];
  const activeMeas = measurements[activeChannel.index] ?? measurements[0];

  // -------------------------------------------------------------
  // Full-screen 3D cube scene (three.js)
  // -------------------------------------------------------------
  useEffect(() => {
    const canvas = glRef.current;
    if (!canvas || typeof window === 'undefined') return;

    const THREE = window.THREE as ThreeAny;
    if (!THREE || !THREE.WebGLRenderer) return;

    let animId = 0;
    let disposed = false;
    let renderer: ThreeAny;
    let scene: ThreeAny;
    let camera: ThreeAny;

    try {
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setClearColor(0x04050a, 1);

      scene = new THREE.Scene();
      camera = new THREE.PerspectiveCamera(42, 1, 0.1, 400);
      camera.position.set(19, 3.5, 16);

      // Orbit state (infra102.html orientation)
      const target = { x: 0, y: -3, z: 0 };
      const spherical = {
        radius: Math.hypot(19, 6.5, 16),
        theta: Math.atan2(19, 16),
        phi: Math.acos(6.5 / Math.hypot(19, 6.5, 16)),
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
        spherical.radius = Math.max(8, Math.min(70, spherical.radius + e.deltaY * 0.03));
        updateCamera();
      };

      canvas.addEventListener('pointerdown', onPointerDown);
      canvas.addEventListener('pointermove', onPointerMove);
      canvas.addEventListener('pointerup', onPointerUp);
      canvas.addEventListener('wheel', onWheel, { passive: false });

      const zb = (b: number) => (b - (NB - 1) / 2) * 0.13;
      const SZ = 9;
      const PX = 32;
      const NP = PX * PX;
      const BASE = -10.2;
      const BARH = 4.8;

      // Backdrop stars
      {
        const n = 700;
        const p = new Float32Array(n * 3);
        let seed = 9;
        const rnd = () => {
          seed = (seed * 16807) % 2147483647;
          return seed / 2147483647;
        };
        for (let i = 0; i < n; i++) {
          const u = rnd() * 2 - 1;
          const t = rnd() * 6.283;
          const q = Math.sqrt(1 - u * u);
          const R = 90;
          p[i * 3] = R * q * Math.cos(t);
          p[i * 3 + 1] = R * u;
          p[i * 3 + 2] = R * q * Math.sin(t);
        }
        const g = new THREE.BufferGeometry();
        g.setAttribute('position', new THREE.BufferAttribute(p, 3));
        scene.add(
          new THREE.Points(
            g,
            new THREE.PointsMaterial({ color: 0x8fa3d0, size: 0.35, transparent: true, opacity: 0.55, sizeAttenuation: true, depthWrite: false }),
          ),
        );
      }

      // 102 sky channel planes
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
          opacity: 0.2,
        });

        const mesh = new THREE.Mesh(planeGeo, mat);
        mesh.position.z = zb(b);
        scene.add(mesh);
        layers.push({ mat, mesh });
      }

      // Detector group frames
      const h = SZ / 2;
      const framePts = [-h, -h, 0, h, -h, 0, h, h, 0, -h, h, 0];
      for (let g = 0; g <= 6; g++) {
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(framePts), 3));
        const line = new THREE.LineLoop(
          geo,
          new THREE.LineBasicMaterial({ color: 0x6f86c8, transparent: true, opacity: 0.3 }),
        );
        line.position.z = zb(g * NC - 0.5);
        scene.add(line);
      }

      // Selected band outline frame
      const selGeo = new THREE.BufferGeometry();
      selGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(framePts), 3));
      const selFrame = new THREE.LineLoop(
        selGeo,
        new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.95 }),
      );
      scene.add(selFrame);

      // Wavelength axis labels
      const makeLabel = (text: string, color = '#9fb4e0') => {
        const c = document.createElement('canvas');
        c.width = 128;
        c.height = 48;
        const g = c.getContext('2d');
        if (g) {
          g.font = '600 26px system-ui, sans-serif';
          g.fillStyle = color;
          g.textAlign = 'center';
          g.textBaseline = 'middle';
          g.fillText(text, 64, 24);
        }
        const t = new THREE.CanvasTexture(c);
        const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: t, transparent: true, depthTest: false }));
        s.scale.set(1.7, 0.64, 1);
        return s;
      };
      const edges = [0.75, 1.11, 1.64, 2.42, 3.82, 4.42, 5.0];
      edges.forEach((e, g) => {
        const s = makeLabel(e.toFixed(2) + (g === 6 ? ' µm' : ''));
        s.position.set(5.6, -4.9, zb(g * NC - 0.5));
        scene.add(s);
      });
      {
        const ax = new THREE.BufferGeometry();
        ax.setAttribute(
          'position',
          new THREE.BufferAttribute(new Float32Array([4.7, -4.7, zb(-0.5), 4.7, -4.7, zb(NB - 0.5)]), 3),
        );
        scene.add(new THREE.Line(ax, new THREE.LineBasicMaterial({ color: 0x6f86c8, transparent: true, opacity: 0.5 })));
      }

      // Spectrum flux pillars (InstancedMesh)
      const bars = new THREE.InstancedMesh(
        new THREE.BoxGeometry(1, 1, 1),
        new THREE.MeshBasicMaterial({ color: 0xffffff }),
        NB,
      );
      bars.frustumCulled = false;
      scene.add(bars);

      // Baseline border loop
      const floorGeo = new THREE.BufferGeometry();
      floorGeo.setAttribute(
        'position',
        new THREE.BufferAttribute(
          new Float32Array([
            -0.8, BASE, zb(-0.5),
            -0.8, BASE, zb(NB - 0.5),
            0.8, BASE, zb(NB - 0.5),
            0.8, BASE, zb(-0.5),
          ]),
          3,
        ),
      );
      scene.add(new THREE.LineLoop(floorGeo, new THREE.LineBasicMaterial({ color: 0x6f86c8, transparent: true, opacity: 0.5 })));

      const dummy = new THREE.Object3D();
      const tmpC = new THREE.Color();
      let maxF = measurements.reduce((mx, m) => Math.max(mx, m.flux_uJy), 10);
      maxF = Math.max(maxF, 1);

      const resize = () => {
        const w = window.innerWidth;
        const ht = window.innerHeight;
        renderer.setSize(w, ht, false);
        camera.aspect = w / ht;
        camera.updateProjectionMatrix();
      };
      resize();
      window.addEventListener('resize', resize);

      const render = () => {
        if (disposed) return;
        const { bandIndex: band, sliceOnly: slice, measurements: meas } = live.current;

        if (autoRotate) {
          spherical.theta += 0.0035;
          updateCamera();
        }

        selFrame.position.z = zb(band);
        for (let i = 0; i < NB; i++) {
          const dd = Math.abs(i - band);
          layers[i].mat.opacity = i === band ? 1 : slice ? 0.035 : 0.05 + 0.22 * Math.exp(-dd / 10);
        }

        for (let k = 0; k < NB; k++) {
          const m = meas[k];
          let hPillar = (Math.max(m.flux_uJy, 0.5) / maxF) * BARH;
          hPillar = Math.max(hPillar, 0.06);
          dummy.position.set(0, BASE + hPillar / 2, zb(k));
          dummy.scale.set(k === band ? 1.7 : 1.2, hPillar, 0.095);
          dummy.updateMatrix();
          bars.setMatrixAt(k, dummy.matrix);

          if (k === band) tmpC.setRGB(1, 1, 1);
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
        disposed = true;
        cancelAnimationFrame(animId);
        window.removeEventListener('resize', resize);
        canvas.removeEventListener('pointerdown', onPointerDown);
        canvas.removeEventListener('pointermove', onPointerMove);
        canvas.removeEventListener('pointerup', onPointerUp);
        canvas.removeEventListener('wheel', onWheel);

        layers.forEach((l) => {
          l.mat.map?.dispose();
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
      console.warn('Three.js initialization failed (WebGL not supported?)', e);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -------------------------------------------------------------
  // 2D spectrum panel (top-right)
  // -------------------------------------------------------------
  useEffect(() => {
    const canvas = specRef.current;
    if (!canvas) return;
    drawSpectrumPlot(canvas, { measurements, comparisonMeasurements, bandIndex, deltaMode, logScale });
  }, [measurements, comparisonMeasurements, bandIndex, deltaMode, logScale]);

  // -------------------------------------------------------------
  // Band scrubber (bottom)
  // -------------------------------------------------------------
  useEffect(() => {
    const canvas = scrubRef.current;
    if (!canvas) return;
    drawBandScrubber(canvas, bandIndex);
  }, [bandIndex]);

  const handleScrub = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = scrubRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    onBandChange(scrubberBandAtX(e.clientX - rect.left, rect.width));
  };

  // Esc closes the overlay (arrows/space are handled by the parent's keydown listener)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const toolBtn = (active: boolean) =>
    cn(
      'h-7 rounded-md border px-2.5 font-mono text-[11px] transition-colors',
      active
        ? 'border-cyan bg-cyan/15 text-cyan'
        : 'border-[rgba(140,170,255,0.28)] text-[#d4e4ff] hover:border-cyan',
    );

  return (
    <div className="fixed inset-0 z-50 bg-[#04050a]" role="dialog" aria-modal="true" aria-label="SPHEREx 102-band data cube">
      <canvas ref={glRef} className="fixed inset-0 block h-full w-full cursor-crosshair" />

      {/* Title + readout (top-left) */}
      <div className={cn(PANEL, 'left-4 top-4 min-w-[220px] px-3 py-2.5')}>
        <div className="flex items-center gap-2">
          <Box className="size-4 text-cyan" aria-hidden="true" />
          <span className="text-[15px] font-semibold tracking-[0.18em]">SPHEREx</span>
          <span className="text-[11px] text-[#7d8fb0]">· 102 bands · 0.75–5.0 µm</span>
        </div>
        <div className="mt-1.5 font-mono text-[11px] tabular-nums">
          <div>
            <span className="font-bold text-white">{activeChannel.wavelength_um.toFixed(3)} µm</span>
            <span className="text-[#7d8fb0]">
              {' '}
              · ch {activeChannel.index + 1}/102 · detector {activeChannel.detector} · R{activeChannel.R}
            </span>
          </div>
          <div className="text-[#7d8fb0]">
            {activeMeas.flux_uJy.toFixed(1)} ± {activeMeas.flux_err_uJy.toFixed(1)} µJy
            {' · '}Candidate #{candidate.ordinal ?? candidate.candidate_id}
          </div>
        </div>
        <div className="mt-1.5 text-[10px] text-[#7d8fb0]">
          drag = orbit · scroll = zoom · drag strip = band · ←/→ = band · space = sweep · esc = close
        </div>
      </div>

      {/* 2D spectrum (top-right) */}
      <div className={cn(PANEL, 'right-4 top-4 hidden p-1.5 sm:block')}>
        <canvas ref={specRef} className="block h-[214px] w-[380px]" />
      </div>

      {/* Tools (bottom-right, above scrubber) */}
      <div className={cn(PANEL, 'bottom-24 right-4 flex items-center gap-1.5 px-2 py-1.5')}>
        <span className="mr-0.5 hidden text-[10px] text-[#7d8fb0] md:inline">view</span>
        <button type="button" onClick={onToggleSlice} className={toolBtn(sliceOnly)} title="Show only the selected band">
          Slice
        </button>
        <button type="button" onClick={onToggleDelta} className={toolBtn(deltaMode)} title="Continuum-subtracted mode">
          Δ
        </button>
        <button type="button" onClick={onToggleLog} className={toolBtn(logScale)} title="Log flux axis">
          Log
        </button>
        <div className="mx-0.5 h-[18px] w-px bg-[rgba(140,170,255,0.2)]" />
        <button type="button" onClick={onToggleSweep} className={toolBtn(isSweeping)} title="Sweep through the bands (space)">
          {isSweeping ? <Pause className="size-3" /> : <Play className="size-3" />}
        </button>
        <button
          type="button"
          onClick={onClose}
          className={toolBtn(false)}
          title="Close fullscreen (esc)"
          aria-label="Close fullscreen cube"
        >
          <X className="size-3.5" />
        </button>
      </div>

      {/* Band scrubber (bottom) */}
      <div className={cn(PANEL, 'bottom-3 left-4 right-4 h-[72px] overflow-hidden p-0')}>
        <canvas
          ref={scrubRef}
          onPointerDown={(e) => {
            (e.target as HTMLCanvasElement).setPointerCapture(e.pointerId);
            handleScrub(e);
          }}
          onPointerMove={(e) => {
            if (e.buttons === 1) handleScrub(e);
          }}
          className="block h-full w-full cursor-ew-resize select-none"
        />
      </div>
    </div>
  );
}
