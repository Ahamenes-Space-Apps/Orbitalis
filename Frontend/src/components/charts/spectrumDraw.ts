import { BANDS, candidateFeatures, CHANNELS, clamp, LR, NB, NC } from './spherex';
import type { ChannelMeasurement } from './spherex';

function fitCanvas(canvas: HTMLCanvasElement, fallbackW: number, fallbackH: number) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const W = canvas.clientWidth || fallbackW;
  const H = canvas.clientHeight || fallbackH;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  return { ctx, W, H };
}

export interface SpectrumPlotOptions {
  measurements: ChannelMeasurement[];
  comparisonMeasurements: ChannelMeasurement[] | null;
  bandIndex: number;
  deltaMode: boolean;
  logScale: boolean;
}

export function drawSpectrumPlot(canvas: HTMLCanvasElement, opts: SpectrumPlotOptions) {
  const { measurements, comparisonMeasurements, bandIndex, deltaMode, logScale } = opts;
  const fit = fitCanvas(canvas, 360, 200);
  if (!fit) return;
  const { ctx, W, H } = fit;

  const ml = 42, mr = 10, mt = 18, mb = 22;
  const pw = W - ml - mr;
  const ph = H - mt - mb;

  const X = (lam: number) => ml + (Math.log(lam / 0.75) / LR) * pw;

  // Calculate flux range (or continuum-subtracted delta range)
  let ymax = 10;
  let ymin = 0;
  const values: number[] = [];

  measurements.forEach((m, idx) => {
    if (deltaMode) {
      // Continuum estimate from neighboring channels (width = 4)
      const left = measurements[Math.max(0, idx - 4)].flux_uJy;
      const right = measurements[Math.min(NB - 1, idx + 4)].flux_uJy;
      const cont = (left + right) * 0.5;
      const delta = m.flux_uJy - cont;
      values.push(delta);
      ymax = Math.max(ymax, Math.abs(delta) + m.flux_err_uJy);
    } else {
      values.push(m.flux_uJy);
      ymax = Math.max(ymax, m.flux_uJy + m.flux_err_uJy);
    }
  });

  if (deltaMode) {
    ymax = Math.max(ymax * 1.25, 5);
    ymin = -ymax;
  } else {
    ymax = Math.max(ymax * 1.15, 10);
    ymin = 0;
  }

  const logLo = Math.max(1, ymax * 0.005);
  const L0 = Math.log10(logLo);
  const L1 = Math.log10(ymax);

  const Y = (v: number) => {
    if (deltaMode) {
      const span = ymax - ymin;
      return mt + ph * (1 - (v - ymin) / span);
    }
    if (logScale) {
      const val = Math.max(v, logLo);
      const norm = (Math.log10(val) - L0) / Math.max(0.1, L1 - L0);
      return mt + ph * (1 - clamp(norm, 0, 1.05));
    }
    return mt + ph * (1 - clamp(v / ymax, 0, 1.05));
  };

  // Detector group background bands (1 to 6)
  const EDGES = [0.75, 1.11, 1.64, 2.42, 3.82, 4.42, 5.0];
  ctx.font = '9px ui-monospace, SFMono-Regular, Menlo, monospace';
  ctx.textAlign = 'center';
  for (let i = 0; i < 6; i++) {
    const x0 = X(EDGES[i]);
    const x1 = X(EDGES[i + 1]);
    if (i % 2 === 0) {
      ctx.fillStyle = 'rgba(255, 255, 255, 0.025)';
      ctx.fillRect(x0, mt, x1 - x0, ph);
    }
    ctx.fillStyle = 'rgba(160, 180, 220, 0.45)';
    ctx.fillText(`D${i + 1}`, (x0 + x1) / 2, mt - 4);
  }

  // Axes
  ctx.strokeStyle = 'rgba(140, 170, 255, 0.22)';
  ctx.lineWidth = 1;
  ctx.strokeRect(ml, mt, pw, ph);

  // X-axis ticks (µm)
  ctx.fillStyle = '#8497bb';
  ctx.font = '9px system-ui, sans-serif';
  ctx.textAlign = 'center';
  for (const tick of [1.0, 1.5, 2.0, 3.0, 4.0, 5.0]) {
    const x = X(tick);
    ctx.beginPath();
    ctx.moveTo(x, mt + ph);
    ctx.lineTo(x, mt + ph + 3);
    ctx.stroke();
    ctx.fillText(tick.toFixed(1), x, mt + ph + 13);
  }
  ctx.fillText('µm', ml + pw - 6, mt + ph + 13);

  // Y-axis ticks & zero line
  ctx.textAlign = 'right';
  if (deltaMode) {
    const yZero = Y(0);
    ctx.strokeStyle = 'rgba(140, 170, 255, 0.35)';
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(ml, yZero);
    ctx.lineTo(ml + pw, yZero);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillText('0', ml - 4, yZero + 3);
    ctx.fillText(`+${Math.round(ymax)}`, ml - 4, mt + 9);
    ctx.fillText(`-${Math.round(ymax)}`, ml - 4, mt + ph - 2);
  } else if (logScale) {
    for (let exp = Math.ceil(L0); exp <= Math.floor(L1); exp++) {
      const val = Math.pow(10, exp);
      const y = Y(val);
      ctx.fillText(val >= 1000 ? `${(val / 1000).toFixed(0)}k` : `${val}`, ml - 4, y + 3);
      ctx.strokeStyle = 'rgba(140, 170, 255, 0.08)';
      ctx.beginPath();
      ctx.moveTo(ml, y);
      ctx.lineTo(ml + pw, y);
      ctx.stroke();
    }
  } else {
    for (let i = 0; i <= 3; i++) {
      const v = (ymax * i) / 3;
      const y = Y(v);
      ctx.fillText(v >= 1000 ? `${(v / 1000).toFixed(1)}k` : `${Math.round(v)}`, ml - 4, y + 3);
      if (i > 0) {
        ctx.strokeStyle = 'rgba(140, 170, 255, 0.08)';
        ctx.beginPath();
        ctx.moveTo(ml, y);
        ctx.lineTo(ml + pw, y);
        ctx.stroke();
      }
    }
  }

  ctx.textAlign = 'left';
  ctx.fillStyle = '#8497bb';
  ctx.fillText(deltaMode ? 'Δ Fν µJy' : 'Fν µJy', 4, 10);

  // Continuum Guide Line
  ctx.save();
  ctx.beginPath();
  if (typeof ctx.rect === 'function') {
    ctx.rect(ml, mt, pw, ph);
    ctx.clip();
  }

  ctx.strokeStyle = deltaMode ? 'rgba(126, 224, 255, 0.25)' : 'rgba(255, 255, 255, 0.25)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  measurements.forEach((m, idx) => {
    const x = X(m.channel.wavelength_um);
    const y = Y(values[idx]);
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Secondary comparison epoch curve (if enabled)
  if (comparisonMeasurements) {
    ctx.strokeStyle = '#f5b942';
    ctx.lineWidth = 1.2;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    comparisonMeasurements.forEach((m, idx) => {
      const x = X(m.channel.wavelength_um);
      const y = Y(deltaMode ? values[idx] * 0.9 : m.flux_uJy);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Draw 102 channel points with 1σ error bars
  measurements.forEach((m, idx) => {
    const val = values[idx];
    const x = X(m.channel.wavelength_um);
    const isSelected = idx === bandIndex;

    // Color logic: delta mode highlights emission in channel color, absorption in cyan
    let colorCss = m.channel.cssColor;
    if (deltaMode) {
      colorCss = val >= 0 ? m.channel.cssColor : '#7ee0ff';
    }

    // Error bar
    if (!deltaMode && m.flux_err_uJy > 0) {
      ctx.strokeStyle = isSelected ? '#ffffff' : colorCss;
      ctx.globalAlpha = m.isReal ? 0.75 : 0.35;
      ctx.lineWidth = isSelected ? 1.4 : 1;
      ctx.beginPath();
      ctx.moveTo(x, Y(val - m.flux_err_uJy));
      ctx.lineTo(x, Y(val + m.flux_err_uJy));
      ctx.stroke();
    }

    // Point marker
    ctx.globalAlpha = isSelected ? 1 : m.isReal ? 0.95 : 0.55;
    ctx.fillStyle = colorCss;
    ctx.beginPath();
    ctx.arc(x, Y(val), isSelected ? 4 : m.isReal ? 2.6 : 1.8, 0, Math.PI * 2);
    ctx.fill();

    if (isSelected) {
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  });

  ctx.restore();

  // Vertical dashed marker for selected band
  const xSel = X(CHANNELS[bandIndex].wavelength_um);
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.75)';
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(xSel, mt);
  ctx.lineTo(xSel, mt + ph);
  ctx.stroke();
  ctx.setLineDash([]);

  // Feature indicator labels at the top
  const featList = candidateFeatures(measurements.map((m) => m.channel.wavelength_um));
  ctx.font = '8.5px ui-monospace, SFMono-Regular, Menlo, monospace';
  ctx.fillStyle = 'rgba(200, 215, 245, 0.75)';
  featList.slice(0, 7).forEach(([name, wave]) => {
    const x = X(wave);
    if (x >= ml + 6 && x <= ml + pw - 6) {
      ctx.strokeStyle = 'rgba(200, 215, 245, 0.3)';
      ctx.beginPath();
      ctx.moveTo(x, mt);
      ctx.lineTo(x, mt + 6);
      ctx.stroke();
      ctx.save();
      ctx.translate(x + 2, mt + 9);
      ctx.rotate(Math.PI / 2);
      ctx.textAlign = 'left';
      ctx.fillText(name, 0, 0);
      ctx.restore();
    }
  });
}

export function drawBandScrubber(canvas: HTMLCanvasElement, bandIndex: number) {
  const fit = fitCanvas(canvas, 360, 56);
  if (!fit) return;
  const { ctx, W, H } = fit;

  const unit = (W - 16) / (NB + 5 * 1.8);
  const tx = (k: number) => 8 + (k + (CHANNELS[k]?.detector - 1) * 1.8 + 0.5) * unit;
  const yb = H - 22;

  for (let k = 0; k < NB; k++) {
    const isSelected = k === bandIndex;
    const barH = isSelected ? 26 : 14;
    ctx.fillStyle = CHANNELS[k].cssColor;
    ctx.globalAlpha = isSelected ? 1 : 0.7;
    ctx.fillRect(tx(k) - unit * 0.35, yb - barH, unit * 0.7, barH);
  }

  // Pointer marker
  const xs = tx(bandIndex);
  ctx.globalAlpha = 1;
  ctx.fillStyle = '#ffffff';
  ctx.beginPath();
  ctx.moveTo(xs, yb + 2);
  ctx.lineTo(xs - 4, yb + 7);
  ctx.lineTo(xs + 4, yb + 7);
  ctx.fill();

  // Detector and wavelength annotations
  ctx.font = '9px ui-monospace, SFMono-Regular, Menlo, monospace';
  ctx.textAlign = 'left';
  ctx.fillStyle = '#8497bb';
  for (let i = 0; i < 6; i++) {
    const x = tx(i * NC) - unit * 0.5;
    ctx.fillText(`${BANDS[i][0].toFixed(2)}µm`, x, H - 6);
  }
  ctx.textAlign = 'right';
  ctx.fillText('5.00', tx(NB - 1) + unit * 0.5, H - 6);
}

export function scrubberBandAtX(x: number, width: number): number {
  const unit = (width - 16) / (NB + 5 * 1.8);
  let best = 0;
  let minDist = 1e9;
  for (let k = 0; k < NB; k++) {
    const targetX = 8 + (k + (CHANNELS[k]?.detector - 1) * 1.8 + 0.5) * unit;
    const dist = Math.abs(targetX - x);
    if (dist < minDist) {
      minDist = dist;
      best = k;
    }
  }
  return best;
}
