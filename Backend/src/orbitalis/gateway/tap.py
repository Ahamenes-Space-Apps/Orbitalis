"""TAP discovery for SPHEREx frames (D-01 decided query shape).

Purpose: Build the ADQL query and parse the VOTable response into FrameRefs.
Inputs: sky position, optional release and MJD time range.
Outputs: ADQL string; ``list[FrameRef]``.
Called by: gateway.client (``find_frames``); tests use a recorded VOTable.

Verified against live IRSA on 2026-11-07 (SDD Appendix D-01):
- ``spherex.plane`` joined to ``spherex.artifact`` on ``planeid``.
- ``time_bounds_lower/upper`` are MJD days; each exposure ~115 s.
- Release lives in ``obs_publisher_did`` (``ivo://irsa.ipac/spherex_qr2?...``)
  and the artifact URI path; ``releasetype`` only says data/meta.
- ``producttype='science'`` selects science products (calibration excluded).
- ``energy_bandpassname`` is ``SPHEREx-D1``..``SPHEREx-D6``.
"""

from __future__ import annotations

import io
import re
from typing import Any

from astropy.io import votable
from astropy.utils.exceptions import AstropyUserWarning

from orbitalis.gateway.schemas import FrameRef

# IRSA split TAP into /sync (immediate VOTable) and /async (job-based 303);
# the old /TAP path 301s and /TAP/ itself is an unknown endpoint. /sync is
# the synchronous doQuery endpoint this client posts to.
IRSA_TAP_URL = "https://irsa.ipac.caltech.edu/TAP/sync"
TAP_TIMEOUT_S = 60.0
TAP_CACHE_TTL_S = 6 * 3600

# All table/column names live here (D-01: single constants block).
_PLANE = "spherex.plane"
_ARTIFACT = "spherex.artifact"
_COLUMNS = (
    "p.obsid",
    "p.obs_publisher_did",
    "p.time_bounds_lower",
    "p.time_bounds_upper",
    "p.time_exposure",
    "p.energy_bandpassname",
    "p.energy_bounds_lower",
    "p.energy_bounds_upper",
    "p.energy_resolvingpower",
    "p.position_samplesize",
    "p.provenance_version",
    "p.calibrationlevel",
    "a.uri",
    "a.producttype",
)

_RELEASE_RE = re.compile(r"spherex[_/](qr\d+|dr\d+)")
_DETECTOR_RE = re.compile(r"SPHEREx-D(\d)")


def build_adql(
    ra: float,
    dec: float,
    *,
    release: str | None = None,
    mjd_start: float | None = None,
    mjd_end: float | None = None,
) -> str:
    """ADQL for planes covering (ra, dec), optionally filtered by release/time.

    ``radius`` is intentionally not a query parameter: SPHEREx plane footprints
    are degrees across, so point coverage is the operative filter; the field
    radius is realised by the cutout size, not the TAP query.
    """
    where = [
        f"1 = CONTAINS(POINT('ICRS', {ra!r}, {dec!r}), p.poly)",
        "a.producttype = 'science'",
    ]
    if release is not None:
        # DIDs come in (at least) two forms — "spherex_qr2?<obsid>" and
        # "spherex_qr3/<obsid>" — so match the release tag without assuming
        # the separator (RS.08: R Dra QR3 returned 0 rows with "qr3?").
        where.append(f"p.obs_publisher_did LIKE '%spherex_{release}%'")
    if mjd_start is not None:
        where.append(f"p.time_bounds_upper >= {mjd_start!r}")
    if mjd_end is not None:
        where.append(f"p.time_bounds_lower <= {mjd_end!r}")
    return (
        f"SELECT {', '.join(_COLUMNS)} FROM {_PLANE} p "
        f"JOIN {_ARTIFACT} a ON a.planeid = p.planeid "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY p.time_bounds_lower, p.obsid, p.energy_bandpassname"
    )


def parse_release(obs_publisher_did: str, uri: str) -> str:
    """Release tag (``qr2``, ``qr3``...) from the DIDs or artifact URI."""
    for text in (obs_publisher_did, uri):
        m = _RELEASE_RE.search(text or "")
        if m:
            return m.group(1)
    return "unknown"


def parse_detector(bandpass: str | None, obs_publisher_did: str) -> int | None:
    """Detector number 1-6 from ``SPHEREx-Dn`` or the DID's trailing ``/Dn``."""
    m = _DETECTOR_RE.search(bandpass or "")
    if m:
        return int(m.group(1))
    m = re.search(r"/D(\d)\b", obs_publisher_did or "")
    return int(m.group(1)) if m else None


def _cell(value: Any) -> Any:
    """VOTable char arrays come back as numpy str/bytes; normalise to python.

    VOTable NULLs arrive as ``np.ma.masked`` — they normalise to ``None``
    so downstream code never sees a masked constant.
    """
    if value is None:
        return None
    try:
        import numpy as np

        if np.ma.is_masked(value):
            return None
    except ImportError:  # pragma: no cover
        pass
    # np.bytes_ subclasses both bytes and np.generic — decode first or it
    # would come back as raw bytes via .item().
    if isinstance(value, bytes):
        return value.decode()
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except ImportError:  # pragma: no cover
        pass
    return value


def row_to_frame_ref(row: dict[str, Any]) -> FrameRef:
    """One VOTable result row -> typed FrameRef."""
    obs_id = str(_cell(row.get("obsid")) or "")
    did = str(_cell(row.get("obs_publisher_did")) or "")
    uri = str(_cell(row.get("uri")) or "")
    bandpass = _cell(row.get("energy_bandpassname"))
    bandpass = str(bandpass) if bandpass is not None else None
    release = parse_release(did, uri)
    detector = parse_detector(bandpass, did)
    detector_suffix = f"D{detector}" if detector is not None else ""
    return FrameRef(
        frame_ref=f"{release}.{obs_id}{detector_suffix}",
        release=release,
        obs_id=obs_id,
        detector=detector,
        uri=uri,
        mjd_start=_required_float(row, "time_bounds_lower"),
        mjd_end=_required_float(row, "time_bounds_upper"),
        bandpass=bandpass,
        energy_min_m=_optional_float(row.get("energy_bounds_lower")),
        energy_max_m=_optional_float(row.get("energy_bounds_upper")),
        resolving_power=_optional_float(row.get("energy_resolvingpower")),
        pixel_scale_arcsec=_optional_float(row.get("position_samplesize")),
        time_exposure_s=_optional_float(row.get("time_exposure")),
        provenance_version=(
            str(v) if (v := _cell(row.get("provenance_version"))) is not None else None
        ),
        calibration_level=_optional_int(row.get("calibrationlevel")),
        product_type=(str(v) if (v := _cell(row.get("producttype"))) is not None else None),
    )


def _required_float(row: dict[str, Any], key: str) -> float:
    """Required numeric TAP column: masked/missing/non-finite is a parse error."""
    import math

    from orbitalis.gateway.errors import TapQueryError

    value = _cell(row.get(key))
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TapQueryError(f"TAP row missing required column {key!r}") from exc
    if not math.isfinite(number):
        raise TapQueryError(f"TAP row column {key!r} is not finite: {number!r}")
    return number


def _optional_float(value: Any) -> float | None:
    value = _cell(value)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    import math

    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    value = _cell(value)
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_votable(payload: bytes) -> list[FrameRef]:
    """Parse a TAP VOTable response into FrameRefs (raises on malformed XML)."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AstropyUserWarning)
        vot = votable.parse(io.BytesIO(payload))
    tables = list(vot.iter_tables())
    if not tables:
        return []
    cols = [f.name for f in tables[0].fields]
    out: list[FrameRef] = []
    for row in tables[0].array:
        out.append(row_to_frame_ref({c: row[c] for c in cols}))
    return out


__all__ = [
    "IRSA_TAP_URL",
    "TAP_CACHE_TTL_S",
    "TAP_TIMEOUT_S",
    "build_adql",
    "parse_detector",
    "parse_release",
    "parse_votable",
    "row_to_frame_ref",
]
