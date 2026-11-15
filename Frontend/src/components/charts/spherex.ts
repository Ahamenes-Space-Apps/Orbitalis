/* ============================================================
   SPHEREx 102-channel instrument layout (from infra102.html)
   6 detector bands x 17 channels = 102 spectral channels
   Resolving power R = 41 / 41 / 41 / 35 / 110 / 130
   ============================================================ */
export const NC = 17;
export const NB = 102;
export const BANDS = [
  [0.75, 1.11, 41],
  [1.11, 1.64, 41],
  [1.64, 2.42, 41],
  [2.42, 3.82, 35],
  [3.82, 4.42, 110],
  [4.42, 5.0, 130],
] as const;

export const LR = Math.log(5 / 0.75);

export interface ChannelInfo {
  index: number;
  wavelength_um: number;
  detector: number;
  R: number;
  rgb: [number, number, number];
  cssColor: string;
}

function hsl2rgb(h: number, s: number, l: number): [number, number, number] {
  h /= 360;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + h * 12) % 12;
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  return [f(0), f(8), f(4)];
}

export const CHANNELS: ChannelInfo[] = (() => {
  const list: ChannelInfo[] = [];
  BANDS.forEach(([a, b, R], g) => {
    const r = Math.pow(b / a, 1 / NC);
    for (let i = 0; i < NC; i++) {
      const k = g * NC + i;
      const lam = a * Math.pow(r, i + 0.5);
      const t = Math.log(lam / 0.75) / LR;
      const rgb = hsl2rgb(250 * (1 - t), 0.95, 0.58);
      list.push({
        index: k,
        wavelength_um: lam,
        detector: g + 1,
        R,
        rgb,
        cssColor: `rgb(${Math.round(rgb[0] * 255)}, ${Math.round(rgb[1] * 255)}, ${Math.round(rgb[2] * 255)})`,
      });
    }
  });
  return list;
})();

/* ============================================================
   Physical models for synthetic fallback (from infra102.html)
   ============================================================ */
export function bbn(l: number, T: number): number {
  const x = 14388 / (l * T);
  const e = Math.expm1(x);
  if (!isFinite(e)) return 0;
  return Math.pow(5100 / T / l, 3) * Math.expm1(2.821) / e;
}

export const G = (l: number, c: number, s: number) => {
  const d = (l - c) / s;
  return Math.exp(-0.5 * d * d);
};

export const clamp = (x: number, a: number, b: number) => Math.max(a, Math.min(b, x));

export function starModel(l: number, T = 3400): number {
  let f = bbn(l, T);
  const cool = clamp((4500 - T) / 1500, 0, 1);
  f *= 1 - 0.12 * cool * G(l, 2.4, 0.08) - 0.15 * cool * G(l, 4.7, 0.1);
  const wat = clamp((3700 - T) / 1000, 0, 1);
  f *= 1 - 0.1 * wat * G(l, 1.4, 0.06) - 0.08 * wat * G(l, 1.9, 0.08);
  return f;
}

export function candidateFeatures(wavelengths: number[]): Array<[string, number]> {
  const potential: Array<[string, number]> = [
    ['Lyα', 0.1216],
    ['Hβ', 0.4861],
    ['[OIII]', 0.5007],
    ['Hα', 0.6563],
    ['Paβ', 1.2818],
    ['H₂O', 1.4],
    ['Paα', 1.8751],
    ['H₂O', 1.9],
    ['Brγ', 2.1655],
    ['CO', 2.38],
    ['H₂O ice', 3.05],
    ['PAH', 3.29],
    ['Brα', 4.0523],
    ['CO₂ ice', 4.27],
    ['CO', 4.67],
  ];
  return potential.filter(([, l]) => l >= 0.75 && l <= 5.0 && wavelengths.some((w) => Math.abs(w - l) < 0.15));
}

export interface ChannelMeasurement {
  channel: ChannelInfo;
  flux_uJy: number;
  flux_err_uJy: number;
  detected: boolean;
  isReal: boolean;
  isSynthetic: boolean;
  masked_bits: string[] | null;
}

export interface EpochVisit {
  id: string;
  label: string;
  mjd: number;
  frameCount: number;
}
