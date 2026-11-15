"""Cutout fetch and in-memory FITS trimming (GW.02, D-27).

Purpose: Build cutout URLs and strip a downloaded SPHEREx cutout down to the
HDUs the pipeline needs — never caching or storing the ~5 MB PSF block.
Inputs: frame ``uri`` from TAP, target ra/dec, size in degrees, raw FITS bytes.
Outputs: cutout URL; trimmed FITS ``bytes``.
Called by: gateway.client ``fetch_cutout``; tests.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import numpy as np
from astropy.io import fits

from orbitalis.gateway.errors import CutoutFetchError
from orbitalis_core.errors import InvalidInputError

_FLAGS_WARNED = False

IRSA_BASE_URL = "https://irsa.ipac.caltech.edu/"

# GW-12 / T-03: cutout fetches may only target allowlisted hosts. TAP-provided
# URIs are paths appended to the base, but the final URL is still checked so a
# manipulated base or URI can never pivot the fetch off IRSA.
IRSA_ALLOWED_HOSTS = "irsa.ipac.caltech.edu"

# HDUs kept from a cutout (D-27): IMAGE, FLAGS, VARIANCE, ZODI pixel arrays and
# the WCS-WAVE binary table. The PSF HDU (~4.9 MB, not cropped by IRSA) and any
# other extensions are dropped. The primary HDU (index 0) is always kept for
# structural validity; the celestial SIP WCS actually lives in the IMAGE
# extension header on real cutouts (verified on a live QR2 file).
_KEEP_HDU_NAMES = frozenset({"IMAGE", "FLAGS", "VARIANCE", "ZODI", "WCS-WAVE"})


def cutout_url(base: str, uri: str, ra: float, dec: float, size_deg: float) -> str:
    """Cutout service URL. ``size`` is degrees by default (D-01 verified)."""
    return f"{base.rstrip('/')}/{uri.lstrip('/')}?center={ra},{dec}d&size={size_deg}"


def check_allowed_host(url: str, allowed_hosts: frozenset[str]) -> None:
    """GW-12: reject cutout URLs whose host is not allowlisted.

    Raises ``CutoutFetchError`` (ORB-GW-002) before any request is made.
    """
    host = (urlsplit(url).hostname or "").lower()
    if host not in allowed_hosts:
        raise CutoutFetchError(
            f"cutout URL host {host!r} is not in the IRSA allowlist", retryable=False
        )


def trim_cutout(payload: bytes) -> bytes:
    """Return a new FITS file keeping only the pipeline HDUs (D-27).

    Raises ``CutoutFetchError`` (ORB-GW-002) when the payload is not a readable
    FITS file or has no IMAGE HDU. Deeper validation is GW.03.
    """
    try:
        with fits.open(io.BytesIO(payload)) as hdul:
            kept: list[fits.hdu.base._BaseHDU] = []
            has_image = False
            for i, hdu in enumerate(hdul):
                name = hdu.name or ""
                if i == 0 or name in _KEEP_HDU_NAMES:
                    if name == "IMAGE":
                        has_image = True
                    kept.append(hdu.copy())
            if not has_image:
                raise CutoutFetchError("cutout has no IMAGE HDU")
            out = fits.HDUList(kept)
            buf = io.BytesIO()
            out.writeto(buf)
            return buf.getvalue()
    except CutoutFetchError:
        raise
    except Exception as exc:
        raise CutoutFetchError(f"cutout is not a readable FITS file: {exc}") from exc


def _hdu_named(hdul: fits.HDUList, name: str) -> fits.hdu.base._BaseHDU | None:
    try:
        return hdul[name]
    except KeyError:
        return None


@dataclass
class CutoutData:
    """Validated contents of one SPHEREx cutout (GW.03).

    ``image`` is in MJy/sr float32; ``flags`` int32; ``variance``/``zodi``
    float32 same shape; ``wavetable`` is the raw WCS-WAVE binary-table HDU
    (parsed later by ``orbitalis_core.wcs``); ``header`` is the IMAGE
    extension header (celestial WCS + *W keys), not the bare PRIMARY
    carrying the celestial SIP WCS.
    """

    image: np.ndarray
    flags: np.ndarray
    variance: np.ndarray | None
    zodi: np.ndarray | None
    wavetable: fits.BinTableHDU | None
    header: fits.Header


def extract_cutout(payload: bytes) -> CutoutData:
    """Validate a (trimmed or full) cutout and extract the science arrays.

    Corrupt FITS, a missing IMAGE HDU, or shape/dtype mismatches raise
    ``InvalidInputError`` (ORB-CORE-001). FLAGS/VARIANCE/ZODI/WCS-WAVE are
    optional per D-27 but shape-checked when present.
    """
    try:
        hdul = fits.open(io.BytesIO(payload))
    except Exception as exc:
        raise InvalidInputError(f"corrupt FITS cutout: {exc}") from exc
    with hdul:
        hdu = _hdu_named(hdul, "IMAGE")
        if hdu is None or hdu.data is None:
            raise InvalidInputError("cutout has no IMAGE HDU")
        image = np.asarray(hdu.data, dtype=np.float32)
        shape = image.shape

        def _array(name: str) -> np.ndarray | None:
            ext = _hdu_named(hdul, name)
            if ext is None or ext.data is None:
                return None
            arr = np.asarray(ext.data)
            if arr.shape != shape:
                raise InvalidInputError(f"{name} shape {arr.shape} != IMAGE shape {shape}")
            return arr

        flags = _array("FLAGS")
        if flags is None:
            # No FLAGS HDU means no flag information at all — zeros would
            # silently pass every pixel as unmasked. Warn loudly; the frame
            # still extracts (D-27 keeps FLAGS optional) but the gap is
            # visible in logs instead of hidden. Logged once per process:
            # a field build fetches up to MAX_FRAMES cutouts and a per-frame
            # warning would flood the log with identical lines.
            global _FLAGS_WARNED
            if not _FLAGS_WARNED:
                _FLAGS_WARNED = True
                logging.getLogger(__name__).warning(
                    "cutout missing FLAGS HDU; extracting with zero flags "
                    "(all pixels unmasked); further occurrences not logged"
                )
        variance = _array("VARIANCE")
        zodi = _array("ZODI")
        wt = _hdu_named(hdul, "WCS-WAVE")
        wavetable = (
            fits.BinTableHDU(data=wt.data.copy(), header=wt.header.copy())
            if isinstance(wt, fits.BinTableHDU)
            else None
        )
        return CutoutData(
            image=image,
            flags=flags if flags is not None else np.zeros(shape, np.int32),
            variance=variance,
            zodi=zodi,
            wavetable=wavetable,
            # The celestial WCS and the *W wavelength keywords live in the
            # IMAGE extension header on real cutouts; PRIMARY is bare.
            header=hdu.header,
        )


def hdu_names(payload: bytes) -> list[str]:
    """Names of HDUs in a FITS payload (test/diagnostic helper)."""
    with fits.open(io.BytesIO(payload)) as hdul:
        return [h.name or f"HDU{i}" for i, h in enumerate(hdul)]


__all__ = [
    "IRSA_BASE_URL",
    "CutoutData",
    "cutout_url",
    "extract_cutout",
    "hdu_names",
    "trim_cutout",
]
