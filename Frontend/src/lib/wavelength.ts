/**
 * Inclusive wavelength selections over stored frame wavelengths.
 *
 * Coverage bins group frames by wavelength rounded to 0.01 µm, so membership
 * uses the same rounding. Otherwise a frame at 0.754 µm could count as inside
 * a 0.75–1.10 µm selection in the chart but not in the filtered scrubber.
 */

export type WavelengthRange = [number, number];

/** SPHEREx band coverage used for the positional wavelength ramp. */
export const SPECTRAL_BAND_MIN_UM = 0.75;
export const SPECTRAL_BAND_MAX_UM = 5.0;
export const SPECTRAL_BAND_COUNT = 102;

export function wavelengthKey(um: number): number {
  return Math.round(um * 100) / 100;
}

/** Centre wavelength of an evenly spaced band on the 0.75–5.0 µm ruler. */
export function bandWavelengthForIndex(
  index: number,
  count = SPECTRAL_BAND_COUNT,
): number {
  if (count < 2) return SPECTRAL_BAND_MIN_UM;
  const clamped = Math.min(count - 1, Math.max(0, index));
  return (
    SPECTRAL_BAND_MIN_UM +
    (clamped / (count - 1)) * (SPECTRAL_BAND_MAX_UM - SPECTRAL_BAND_MIN_UM)
  );
}

/** Nearest band index for a wavelength on the 0.75–5.0 µm ruler. */
export function bandIndexForWavelength(
  um: number,
  count = SPECTRAL_BAND_COUNT,
): number {
  if (!Number.isFinite(um) || count < 2) return 0;
  const fraction =
    (um - SPECTRAL_BAND_MIN_UM) / (SPECTRAL_BAND_MAX_UM - SPECTRAL_BAND_MIN_UM);
  return Math.min(count - 1, Math.max(0, Math.round(fraction * (count - 1))));
}

/**
 * Hue for the active wavelength on the 0.75–5.0 µm band ruler.
 *
 * This is a positional rainbow cue (violet at the short end, red at the long
 * end), not a calibrated colour: the infrared light itself is invisible.
 */
export function spectralHueForWavelength(um: number): number {
  if (!Number.isFinite(um)) return 0;
  const t = Math.min(
    1,
    Math.max(
      0,
      (um - SPECTRAL_BAND_MIN_UM) / (SPECTRAL_BAND_MAX_UM - SPECTRAL_BAND_MIN_UM),
    ),
  );
  return 250 * (1 - t);
}

/** CSS colour for the active wavelength tick. */
export function spectralColorForWavelength(um: number): string {
  return `hsl(${spectralHueForWavelength(um).toFixed(1)}, 72%, 62%)`;
}

export function inWavelengthRange(
  value: number,
  range: WavelengthRange | null | undefined,
): boolean {
  if (!range) return true;
  const low = wavelengthKey(Math.min(range[0], range[1]));
  const high = wavelengthKey(Math.max(range[0], range[1]));
  const key = wavelengthKey(value);
  return key >= low && key <= high;
}

export function normalizeWavelengthRange(
  range: WavelengthRange | null | undefined,
): WavelengthRange | null {
  if (!range) return null;
  const low = wavelengthKey(Math.min(range[0], range[1]));
  const high = wavelengthKey(Math.max(range[0], range[1]));
  if (!Number.isFinite(low) || !Number.isFinite(high) || low > high) return null;
  return [low, high];
}


