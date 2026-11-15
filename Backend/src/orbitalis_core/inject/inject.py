"""PSF-based synthetic source injection into frame copies.

Purpose: VAL.01 / FR-VAL-01 — inject moving point sources into copies of
aligned frame arrays for injection-and-recovery validation, without ever
mutating the inputs.
Inputs: ``Sequence[Frame]`` (shiftstack frames: image, variance, time),
``Sequence[InjectedSource]`` truth definitions, ``GaussianPSF``.
Outputs: New ``list[Frame]`` with injected flux; inputs untouched.
Called by: ``orbitalis.research`` validation handlers (VAL.04); tests.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.special import erf

from orbitalis_core.errors import InvalidInputError
from orbitalis_core.shiftstack.models import Frame

__all__ = ["GaussianPSF", "InjectedSource", "flux_for_snr", "inject"]

# Evaluate the Gaussian only inside this many sigma of the centre; beyond it
# the contribution is < ~4e-6 of peak and cannot affect any downstream metric.
_PSF_CUTOFF_SIGMA = 5.0
_SQRT2 = math.sqrt(2.0)


def _pixel_fractions(centres: np.ndarray, x0: float, sigma_px: float) -> np.ndarray:
    """Fraction of a unit 1-D Gaussian inside each unit-width pixel.

    Pixel ``i`` spans ``[i - 0.5, i + 0.5]``; the integral of the Gaussian
    over that interval is a difference of error functions. This is the exact
    pixel-integrated profile, so a source's flux is distributed according to
    how much of it actually falls on each pixel rather than being point-
    sampled at pixel centres.
    """
    hi = (centres + 0.5 - x0) / (sigma_px * _SQRT2)
    lo = (centres - 0.5 - x0) / (sigma_px * _SQRT2)
    return 0.5 * (erf(hi) - erf(lo))


@dataclass(frozen=True)
class GaussianPSF:
    """Pixel-integrated circular Gaussian PSF with ``sigma_px`` pixels.

    ``evaluate`` returns the exact pixel-integrated, unit-sum profile on the
    pixel grid, so ``flux * profile`` adds exactly ``flux`` (image-unit x
    pixel) to the frame away from edges and mask boundaries.

    Pixel integration (rather than point-sampling the profile at pixel
    centres) injects the physically correct flux distribution and never
    loses flux between pixel centres, which matters for the critically
    sampled or undersampled PSFs of real survey data. For SPHEREx the
    measured FWHM is about 6 arcsec, i.e. about 0.98 px at 6.15 arcsec/px,
    so ``sigma_px`` is close to 0.45 and the source is undersampled. An
    undersampled PSF still has a sub-pixel-phase-dependent peak (between
    :attr:`peak` and about 44% of it at a pixel corner), because a point
    source landing between pixels is split; the integrated profile at least
    guarantees that the split conserves flux and that :attr:`peak` is the
    exact achievable maximum.
    """

    sigma_px: float

    def __post_init__(self) -> None:
        if not np.isfinite(self.sigma_px) or self.sigma_px <= 0:
            raise InvalidInputError("psf sigma_px must be a positive finite number")

    @property
    def peak(self) -> float:
        """Fraction of the unit flux landing on the pixel holding the centre.

        This is the achievable on-grid peak for a source whose centroid sits
        exactly at a pixel centre, and it is what :func:`flux_for_snr`
        inverts to convert a peak-pixel SNR into a total flux.
        """
        return float(erf(0.5 / (self.sigma_px * _SQRT2)) ** 2)

    def evaluate(
        self,
        xx: np.ndarray,
        yy: np.ndarray,
        x0: float,
        y0: float,
    ) -> np.ndarray:
        """Unit-sum integrated Gaussian centred at (x0, y0) on (xx, yy)."""
        return _pixel_fractions(xx, x0, self.sigma_px) * _pixel_fractions(yy, y0, self.sigma_px)


@dataclass(frozen=True)
class InjectedSource:
    """One injected moving point source (truth record for VAL.02 matching).

    ``x0``/``y0`` are pixel coordinates at ``reference_time`` (same units as
    ``Frame.time``, conventionally MJD days); ``vx``/``vy`` are pixel/day.
    ``flux`` is the total injected flux in image-unit x pixel; the on-grid
    peak is ``flux / (2*pi*sigma_px**2)``. ``label`` is an opaque truth id.
    """

    x0: float
    y0: float
    vx: float
    vy: float
    flux: float
    reference_time: float
    label: str | None = None

    def __post_init__(self) -> None:
        for name in ("x0", "y0", "vx", "vy", "flux", "reference_time"):
            v = getattr(self, name)
            if not np.isfinite(v):
                raise InvalidInputError(f"injected source {name} must be finite")
        if self.flux <= 0:
            raise InvalidInputError("injected source flux must be positive")

    def position_at(self, time: float) -> tuple[float, float]:
        """Pixel position of the source at ``time`` (days)."""
        dt = float(time) - self.reference_time
        return self.x0 + self.vx * dt, self.y0 + self.vy * dt


def flux_for_snr(
    snr: float,
    variance: float | np.ndarray,
    psf: GaussianPSF,
) -> float:
    """Convert a peak-pixel SNR to injected flux (image-unit·pixel).

    Approximation chain: the injected on-grid peak is ``flux * psf.peak``
    and the detection SNR is ``peak / sqrt(variance)``, so
    ``flux = snr * sqrt(variance) / psf.peak`` with ``psf.peak`` the
    pixel-integrated fraction of the flux in the central pixel
    (:attr:`GaussianPSF.peak`).

    Caveats (PROVISIONAL pending VAL calibration):

    - ``psf.peak`` is the peak for a source centred on a pixel. An
      undersampled PSF (SPHEREx at 6.15 arcsec/px) still has a
      sub-pixel-phase-dependent peak: the brightest pixel holds between
      ~0.44 (source on a pixel corner) and 1.0 (source on a pixel centre)
      of ``psf.peak``. Pixel integration at least conserves flux and makes
      ``psf.peak`` the exact achievable maximum, so the label is an upper
      bound on the realised peak and completeness is conservative, rather
      than the factor-of-two swing of a point-sampled narrow profile.
    - Assumes a background-subtracted peak; preprocessing (15 px high-pass,
      difference imaging) rescales the noise, so the SNR↔flux mapping is
      only approximate under variant E/F (D-24 calibration caveat).
    - ``variance`` may be a scalar or a per-pixel map; an array is reduced
      by the median of finite positive values.
    """
    if not isinstance(psf, GaussianPSF):
        raise InvalidInputError("psf must be a GaussianPSF")
    snr_f = float(snr)
    if not np.isfinite(snr_f) or snr_f <= 0:
        raise InvalidInputError("snr must be a positive finite number")
    if isinstance(variance, np.ndarray):
        finite = variance[np.isfinite(variance) & (variance > 0)]
        if finite.size == 0:
            raise InvalidInputError("variance map has no finite positive values")
        var = float(np.median(finite))
    else:
        var = float(variance)
    if not np.isfinite(var) or var <= 0:
        raise InvalidInputError("variance must be positive and finite")
    return snr_f * math.sqrt(var) / psf.peak


def inject(
    frames: Sequence[Frame],
    sources: Sequence[InjectedSource],
    psf: GaussianPSF,
) -> list[Frame]:
    """Return copies of ``frames`` with ``sources`` added at each epoch.

    The function is deterministic: identical inputs give bit-identical
    outputs (the experiment seed lives in truth generation, not here).
    Flux is added only where the frame image is finite — masked (NaN)
    pixels stay NaN and receive no flux. Pixels whose centre lies outside
    the frame contribute their off-frame flux loss, matching what a real
    partially-off-frame source would do.

    Raises ``InvalidInputError`` on malformed input. An all-NaN frame image
    is passed through unchanged (a fully masked frame is legitimate input,
    not a computation failure).
    """
    if not frames:
        raise InvalidInputError("inject requires at least one frame")
    if not isinstance(psf, GaussianPSF):
        raise InvalidInputError("psf must be a GaussianPSF")

    srcs = list(sources)
    for s in srcs:
        if not isinstance(s, InjectedSource):
            raise InvalidInputError("sources must be InjectedSource instances")

    out: list[Frame] = []
    grids: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
    for fr in frames:
        img = np.asarray(fr.image)
        if img.ndim != 2:
            raise InvalidInputError("frame image must be 2-D")
        new_img = img.astype(np.float64, copy=True)
        h, w = img.shape
        key = (h, w)
        if srcs and key not in grids:
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
            grids[key] = (xx, yy)
        xx, yy = grids.get(key, (None, None))
        for s in srcs:
            x0, y0 = s.position_at(float(fr.time))
            # Bounding box at the PSF cutoff keeps large frames cheap.
            r = _PSF_CUTOFF_SIGMA * psf.sigma_px
            x_lo = max(0, int(np.floor(x0 - r)))
            x_hi = min(w, int(np.ceil(x0 + r)) + 1)
            y_lo = max(0, int(np.floor(y0 - r)))
            y_hi = min(h, int(np.ceil(y0 + r)) + 1)
            if x_lo >= x_hi or y_lo >= y_hi:
                continue  # entirely off-frame: flux is lost off-frame
            sub_x = xx[y_lo:y_hi, x_lo:x_hi]
            sub_y = yy[y_lo:y_hi, x_lo:x_hi]
            prof = psf.evaluate(sub_x, sub_y, x0, y0)
            region = new_img[y_lo:y_hi, x_lo:x_hi]
            finite = np.isfinite(region)
            region[finite] += s.flux * prof[finite]
        out.append(
            Frame(
                image=new_img,
                time=float(fr.time),
                variance=(
                    None
                    if fr.variance is None
                    else np.asarray(fr.variance, dtype=np.float64).copy()
                ),
                frame_id=fr.frame_id,
            )
        )
    return out
