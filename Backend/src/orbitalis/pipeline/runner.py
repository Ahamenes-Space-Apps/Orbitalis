"""Field builder: gateway -> core -> store with SSE progress (PL.01).

Purpose: Orchestrate a cold field build per SDD §6.4/§9: resolve frames via
TAP, fetch+validate cutouts, align to a common grid, apply flags/stretch,
persist frames/groups, and stream events. Single-flight per ``field_id``;
cancellation checked between frames; failed cutouts are skipped with a
warning (degradation rung 2); an open breaker serves already-stored frames
(rung 3).
Inputs: ``Database``, ``IrsaGateway``, ``EventBus``.
Outputs: field/frame/group rows and stream events.
Called by: api routers (ensure_field/subscribe/cancel), main.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from astropy.wcs import WCS
from PIL import Image
from scipy import ndimage

from orbitalis.eventbus import EventBus
from orbitalis.gateway import (
    CircuitOpenError,
    FrameRef,
    IrsaGateway,
)
from orbitalis.gateway.cutout import extract_cutout
from orbitalis.pipeline.schemas import FieldParams
from orbitalis.pipeline.storecache import (
    StoreTapCache,
    cutout_key,
    get_cached_cutout,
    store_cutout,
)
from orbitalis.store import (
    Database,
    add_group_frame,
    encode_frame_array,
    get_field,
    list_frames,
    make_field_id,
    put_frame,
    put_group,
    set_field_grid,
    set_field_status,
    upsert_field,
)
from orbitalis_core.align import to_common_grid, to_common_grid_flags, to_common_grid_variance
from orbitalis_core.flags import apply_default_mask
from orbitalis_core.stretch import (
    StretchParams,
    StretchState,
    apply_stretch,
    initial_stretch,
)
from orbitalis_core.wcs import WaveTab, bandwidth_at, wavelength_at

MAX_FRAMES = int(os.environ.get("ORB_MAX_FRAMES", "120"))
MAX_RADIUS_DEG = 0.5
MAX_ACTIVE_FIELDS = int(os.environ.get("ORB_MAX_ACTIVE_FIELDS", "3"))
GRACE_S = float(os.environ.get("ORB_FIELD_GRACE_S", "20"))
STRETCH_FIX_N = int(os.environ.get("ORB_STRETCH_FIX_N", "8"))
DEFAULT_RELEASE = (
    "qr3"  # D-01 2026-11-13: IRSA retired spherex_qr2 DIDs (now dr1); qr2 filters match 0 rows
)
GRID_PIXEL_SCALE_ARCSEC = 6.15  # ~SPHEREx native pixel; grid is params-derived
# Search-pad constants: the D-17 prior radius is the fastest motion the
# default grid searches; the margin keeps sub-pixel edge effects inside.
SEARCH_PAD_MAX_RATE_ARCSEC_PER_DAY = 8.0  # == DEFAULT_PRIOR_RADIUS_ARCSEC_PER_DAY
SEARCH_PAD_MARGIN_PX = 5
PREFETCH = 8  # cutout fetches in flight per chunk (gateway limiter caps at 4)


class PipelineError(Exception):
    code = "ORB-PL-001"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PipelineCapError(PipelineError):
    code = "ORB-PL-002"


@dataclass
class _Run:
    task: asyncio.Task[None] | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: int = 0
    grace_task: asyncio.Task[None] | None = None


def _delivery_order(frames: list[FrameRef]) -> list[FrameRef]:
    """§9.4 progressive order: spread across wavelength (detector) and time.

    Round-robin across detectors after sorting each detector's frames by MJD
    gives an early mix of bands and epochs before filling gaps.
    """
    by_det: dict[int, list[FrameRef]] = {}
    for ref in frames:
        by_det.setdefault(ref.detector or 0, []).append(ref)
    for group in by_det.values():
        group.sort(key=lambda r: r.mjd_start)
    ordered: list[FrameRef] = []
    queues = [by_det[k] for k in sorted(by_det)]
    while any(queues):
        for q in queues:
            if q:
                ordered.append(q.pop(0))
    return ordered


def _target_grid(
    params: FieldParams, radius_deg: float | None = None
) -> tuple[WCS, tuple[int, int], dict[str, Any]]:
    """Deterministic common grid derived from field params only (PL.01 review).

    Independent of which frame is processed first and stable across
    restarts; the definition is persisted on the field row.
    ``radius_deg`` overrides ``params.radius_deg`` for search-padded fields.
    """
    radius = params.radius_deg if radius_deg is None else radius_deg
    scale_deg = GRID_PIXEL_SCALE_ARCSEC / 3600.0
    n = int(np.ceil(radius * 2 / scale_deg))
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix = [n / 2 + 0.5, n / 2 + 0.5]
    w.wcs.crval = [params.ra, params.dec]
    w.wcs.cdelt = [-scale_deg, scale_deg]
    spec = {
        "ra": params.ra,
        "dec": params.dec,
        "pixel_scale_arcsec": GRID_PIXEL_SCALE_ARCSEC,
        "shape": [n, n],
    }
    return w, (n, n), spec


def _wcs_from_grid(spec: dict[str, Any]) -> tuple[WCS, tuple[int, int]]:
    scale_deg = float(spec["pixel_scale_arcsec"]) / 3600.0
    n = int(spec["shape"][0])
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix = [n / 2 + 0.5, n / 2 + 0.5]
    w.wcs.crval = [float(spec["ra"]), float(spec["dec"])]
    w.wcs.cdelt = [-scale_deg, scale_deg]
    return w, (n, n)


def _background_subtract(aligned: np.ndarray) -> tuple[np.ndarray, float | None]:
    """D-26/CORE.06a: remove the per-frame robust background for display.

    SPHEREx background varies with wavelength within a bandpass, so a single
    frozen stretch on raw MJy/sr bottom-clips frames whose background sits
    below the fit range (measured up to 87% of pixels on real QR2 cutouts).
    Subtracting the per-frame median makes the stretch wavelength-robust.
    Display-only: ``array_blob`` keeps the aligned MJy/sr data untouched.
    Returns ``(display, background)``; ``background`` is ``None`` when the
    frame has no finite pixels.
    """
    finite = aligned[np.isfinite(aligned)]
    if finite.size == 0:
        return aligned, None
    bg = float(np.median(finite))
    return aligned - bg, bg


def _png_bytes(aligned: np.ndarray, params: Any) -> bytes:
    """LA-mode PNG: alpha 0 where the pixel is masked (NaN)."""
    stretched = apply_stretch(aligned, params)
    valid = np.isfinite(aligned)
    gray = np.zeros(aligned.shape, np.uint8)
    gray[valid] = (np.clip(stretched[valid], 0.0, 1.0) * 255).astype(np.uint8)
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    img = Image.fromarray(np.dstack([gray, alpha]), mode="LA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _dilate_invalid(aligned: np.ndarray) -> np.ndarray:
    """PL.01/DS spec §7: grow the invalid footprint by 1 px conservatively."""
    invalid = ~np.isfinite(aligned)
    grown = ndimage.binary_dilation(invalid, iterations=1)
    out = aligned.copy()
    out[grown] = np.nan
    return out


class PipelineRunner:
    """Single-flight field builds with subscriber-driven lifecycle."""

    def __init__(self, db: Database, gateway: IrsaGateway, bus: EventBus | None = None) -> None:
        self.db = db
        self.gateway = gateway
        if gateway.cache is None:
            gateway.cache = StoreTapCache(db)
        self.bus = bus or EventBus()
        self._runs: dict[str, _Run] = {}
        self._grids: dict[str, tuple[WCS, tuple[int, int]]] = {}
        self._pad_deg: dict[str, float] = {}
        self._stretches: dict[str, StretchState] = {}
        self._lock = asyncio.Lock()

    # -- public API --------------------------------------------------------

    def field_id_for(self, params: FieldParams) -> str:
        return make_field_id(params.id_dict())

    async def ensure_field(self, params: FieldParams) -> str:
        """Upsert the field row and start/attach its build. Returns field_id."""
        if params.radius_deg > MAX_RADIUS_DEG:
            raise PipelineCapError(f"radius {params.radius_deg} exceeds {MAX_RADIUS_DEG} deg")
        field_id = self.field_id_for(params)
        await upsert_field(self.db, field_id, params.model_dump(mode="json"), preset=params.preset)
        # A completed field is final: re-ensuring must not flip it back to
        # 'running' and rebuild (bake_demo re-runs ensure_field per preset;
        # the rebuild raced the job submissions and ended 'cancelled').
        existing = await get_field(self.db, field_id)
        if (existing or {}).get("status") != "completed":
            await self.ensure_run(field_id)
        return field_id

    async def ensure_run(self, field_id: str) -> _Run:
        """Start the build task if none is active; attach otherwise."""
        async with self._lock:
            run = self._runs.get(field_id)
            if run is not None and run.task is not None and not run.task.done():
                return run
            active = sum(1 for r in self._runs.values() if r.task is not None and not r.task.done())
            if active >= MAX_ACTIVE_FIELDS:
                raise PipelineCapError(f"too many active fields (max {MAX_ACTIVE_FIELDS})")
            run = _Run()
            self._runs[field_id] = run
            # Mark queued before the task exists: callers polling the field
            # status must not see a stale terminal state ('cancelled'/'failed')
            # from a previous build while this one is being scheduled.
            await set_field_status(self.db, field_id, "queued")
            run.task = asyncio.create_task(self._build(field_id, run))
            return run

    async def subscribe(self, field_id: str, *, after_id: int = 0):
        """Stream field events; last unsubscribe starts the grace timer."""
        run = self._runs.get(field_id)
        if run is not None and run.grace_task is not None:
            run.grace_task.cancel()
            run.grace_task = None
        if run is not None:
            run.subscribers += 1
        try:
            async for event in self.bus.subscribe(field_id, after_id=after_id):
                yield event
        finally:
            if run is not None:
                run.subscribers -= 1
                if run.subscribers <= 0 and run.task is not None and not run.task.done():
                    run.grace_task = asyncio.create_task(self._grace(field_id, run))

    async def cancel(self, field_id: str) -> None:
        run = self._runs.get(field_id)
        if run is not None:
            run.cancel.set()
            if run.task is not None and not run.task.done():
                await set_field_status(self.db, field_id, "cancelled")

    async def close(self) -> None:
        for run in self._runs.values():
            run.cancel.set()
            if run.task is not None:
                run.task.cancel()
        for run in self._runs.values():
            if run.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await run.task

    # -- build ---------------------------------------------------------------

    async def _grace(self, field_id: str, run: _Run) -> None:
        await asyncio.sleep(GRACE_S)
        if run.subscribers <= 0 and run.task is not None and not run.task.done():
            run.cancel.set()

    async def _emit(self, field_id: str, event_type: str, data: dict[str, Any]) -> None:
        await self.bus.publish(field_id, event_type, data)

    async def _build(self, field_id: str, run: _Run) -> None:
        try:
            row = await get_field(self.db, field_id)
            if row is None:
                raise PipelineError(f"unknown field {field_id}")
            params = FieldParams(**row["params"])
            await set_field_status(self.db, field_id, "running")
            cached = await list_frames(self.db, field_id)
            await self._emit(
                field_id,
                "snapshot",
                {
                    "field_id": field_id,
                    "status": "running",
                    "params": params.model_dump(mode="json"),
                    "frames_done": len(cached),
                },
            )
            await self._emit(field_id, "progress", {"stage": "resolve", "done": 0, "total": 1})

            frames = await self.gateway.find_frames(
                params.ra,
                params.dec,
                params.radius_deg,
                release=params.release or DEFAULT_RELEASE,
                mjd_start=params.mjd_start,
                mjd_end=params.mjd_end,
            )
            if params.detectors is not None:
                wanted = set(params.detectors)
                frames = [f for f in frames if f.detector in wanted]
            frames = sorted(frames, key=lambda r: r.mjd_start)
            if len(frames) > MAX_FRAMES:
                # Truncating to the earliest MAX_FRAMES silently drops all
                # later epochs — arbitrary for dense fields. Thin evenly in
                # MJD instead so the cap keeps coverage of the whole window.
                mjds = np.array([float(f.mjd_start) for f in frames])
                targets = np.linspace(mjds[0], mjds[-1], MAX_FRAMES)
                keep: list[int] = []
                used: set[int] = set()
                for t in targets:
                    for idx in np.argsort(np.abs(mjds - t), kind="stable"):
                        i = int(idx)
                        if i not in used:
                            used.add(i)
                            keep.append(i)
                            break
                frames = [frames[i] for i in sorted(keep)]
            else:
                frames = frames[:MAX_FRAMES]
            await self._emit(field_id, "found", {"frames": len(frames)})
            if not frames:
                await self._emit(field_id, "done", {"frames": 0})
                await set_field_status(self.db, field_id, "completed")
                return

            self._pad_deg[field_id] = self._search_pad_deg(params, frames)

            processed = failed = 0
            breaker_exc: CircuitOpenError | None = None
            first_exc: BaseException | None = None
            done_refs = {f["frame_ref"] for f in await list_frames(self.db, field_id)}
            ordered = _delivery_order(frames)
            for i in range(0, len(ordered), PREFETCH):
                chunk = ordered[i : i + PREFETCH]
                # prefetch cutouts concurrently; the gateway limiter bounds
                # real concurrency at ORB_IRSA_CONCURRENCY
                payloads = await asyncio.gather(
                    *(self._fetch_payload(field_id, params, r) for r in chunk),
                    return_exceptions=True,
                )
                for ref, payload in zip(chunk, payloads, strict=True):
                    if run.cancel.is_set():
                        await set_field_status(self.db, field_id, "cancelled")
                        await self._emit(
                            field_id, "done", {"status": "cancelled", "frames": processed}
                        )
                        return
                    if ref.frame_ref in done_refs:
                        processed += 1  # cache-first: already built, reuse it
                    elif isinstance(payload, BaseException):
                        failed += 1  # rung 2: skip the frame, keep building
                        if isinstance(payload, CircuitOpenError) and breaker_exc is None:
                            breaker_exc = payload
                        elif first_exc is None:
                            first_exc = payload
                        await self._emit(
                            field_id,
                            "warning",
                            {
                                "code": getattr(payload, "code", "ORB-PL-001"),
                                "message": f"{ref.frame_ref}: {payload}",
                            },
                        )
                    else:
                        try:
                            await self._process_payload(field_id, params, ref, payload)
                            processed += 1
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            # corrupt FITS, all-masked frame, reprojection
                            # failure: skip this frame, keep building (rung 2)
                            failed += 1
                            if first_exc is None:
                                first_exc = exc
                            await self._emit(
                                field_id,
                                "warning",
                                {
                                    "code": getattr(exc, "code", "ORB-PL-001"),
                                    "message": f"{ref.frame_ref}: {exc}",
                                },
                            )
                    await self._emit(
                        field_id,
                        "progress",
                        {"stage": "frames", "done": processed + failed, "total": len(frames)},
                    )

            if processed == 0 and failed:
                # Nothing usable was produced: an open breaker means IRSA is
                # down (rung 3 — serve cached frames or fail ORB-GW-003);
                # otherwise the field itself failed and must not end
                # 'completed' with zero frames.
                if breaker_exc is not None:
                    raise breaker_exc
                assert first_exc is not None
                raise first_exc
            if breaker_exc is not None:
                await self._emit(
                    field_id,
                    "warning",
                    {
                        "code": "ORB-GW-003",
                        "message": "IRSA became unavailable; field is partial",
                    },
                )
            await self._finalize_groups(field_id)
            await set_field_status(self.db, field_id, "completed")
            await self._emit(field_id, "done", {"frames": processed, "skipped": failed})
        except asyncio.CancelledError:
            await set_field_status(self.db, field_id, "cancelled")
            raise
        except CircuitOpenError as exc:
            cached = await list_frames(self.db, field_id)
            if cached:
                # rung 3: breaker open, serve what is already stored
                await self._emit(
                    field_id,
                    "warning",
                    {"code": "ORB-GW-003", "message": "IRSA unavailable; serving cached frames"},
                )
                await set_field_status(self.db, field_id, "completed")
                await self._emit(field_id, "done", {"frames": len(cached), "cached": True})
            else:
                await set_field_status(self.db, field_id, "failed")
                await self._emit(field_id, "error", {"code": exc.code, "message": exc.message})
        except Exception as exc:
            await set_field_status(self.db, field_id, "failed")
            await self._emit(
                field_id, "error", {"code": getattr(exc, "code", "ORB-PL-001"), "message": str(exc)}
            )

    @staticmethod
    def _search_pad_deg(params: FieldParams, frames: list[FrameRef]) -> float:
        """Padding (deg) so the fastest searched motion stays inside frames.

        ``pad = max_rate * baseline + margin_px``, where baseline is the
        kept frames' MJD span and margin is ``SEARCH_PAD_MARGIN_PX`` grid
        pixels. Returns 0 when ``search_pad`` is off or the baseline is
        unknown.
        """
        if not params.search_pad:
            return 0.0
        starts = [float(f.mjd_start) for f in frames]
        ends = [float(f.mjd_end) for f in frames]
        baseline = max(ends) - min(starts)
        if not np.isfinite(baseline) or baseline <= 0:
            return 0.0
        pad_arcsec = (
            SEARCH_PAD_MAX_RATE_ARCSEC_PER_DAY * baseline
            + SEARCH_PAD_MARGIN_PX * GRID_PIXEL_SCALE_ARCSEC
        )
        return pad_arcsec / 3600.0

    def _radius_deg(self, field_id: str, params: FieldParams) -> float:
        """Effective field radius including any search pad."""
        return params.radius_deg + self._pad_deg.get(field_id, 0.0)

    async def _fetch_payload(self, field_id: str, params: FieldParams, ref: FrameRef) -> bytes:
        """Cutout bytes from cache or gateway (stores new fetches)."""
        size_deg = self._radius_deg(field_id, params) * 2
        key = cutout_key(ref.frame_ref, params.ra, params.dec, size_deg)
        payload = await get_cached_cutout(self.db, key)
        if payload is None:
            payload = await self.gateway.fetch_cutout(ref.uri, params.ra, params.dec, size_deg)
            await store_cutout(self.db, key, payload)
        return payload

    async def _grid_for(self, field_id: str, params: FieldParams):
        """Persisted deterministic common grid; built once per field."""
        cached = self._grids.get(field_id)
        if cached is not None:
            return cached
        row = await get_field(self.db, field_id)
        if row is not None and row.get("grid"):
            grid = _wcs_from_grid(row["grid"])
        else:
            w, shape, spec = _target_grid(params, self._radius_deg(field_id, params))
            await set_field_grid(self.db, field_id, spec)
            grid = (w, shape)
        self._grids[field_id] = grid
        return grid

    async def _process_payload(
        self,
        field_id: str,
        params: FieldParams,
        ref: FrameRef,
        payload: bytes,
    ) -> None:
        data, wcs, masked = await asyncio.to_thread(self._extract_arrays, payload)

        # wavelength at the NATIVE cutout centre (not the aligned grid —
        # off by up to ~22 nm otherwise), per D-28
        wavelength_um = bandwidth_um = None
        if data.wavetable is not None:
            wavelength_um, bandwidth_um = await asyncio.to_thread(
                _wavelength_pair,
                data.header,
                data.wavetable,
                float(masked.shape[1] // 2),
                float(masked.shape[0] // 2),
            )

        # conservative invalid-footprint dilation BEFORE reprojection
        # (DS §7); the regrid itself spreads NaN further, so nothing is
        # dilated afterwards
        masked = _dilate_invalid(masked)
        target_wcs, target_shape = await self._grid_for(field_id, params)
        aligned, _footprint = await asyncio.to_thread(
            to_common_grid, masked, wcs, target_wcs, target_shape
        )

        # CORE.04a / D-29: variance resampled with squared bilinear weights
        # and flags as a conservative stencil OR, both on the common grid.
        # Variance is NaN wherever the final image footprint is invalid so
        # the two share one mask; non-positive values are dropped to NaN
        # (shiftstack weights by 1/variance — a zero would dominate a trial).
        var_aligned = None
        if data.variance is not None:
            var_in = data.variance.astype(np.float64)
            var_in = np.where(
                np.isfinite(var_in) & (var_in > 0) & np.isfinite(masked), var_in, np.nan
            )
            var_aligned = await asyncio.to_thread(
                to_common_grid_variance, var_in, wcs, target_wcs, target_shape
            )
            if not np.isfinite(var_aligned).any():
                var_aligned = None
        flags_aligned = await asyncio.to_thread(
            to_common_grid_flags, data.flags, wcs, target_wcs, target_shape
        )

        # stretch: provisional (fit on frame 1) until fix_n, then frozen.
        # D-26/CORE.06a: fit and render on background-subtracted arrays so
        # wavelength-dependent sky background cannot push later frames'
        # pixels below the frozen `lo` (bottom-clipping measured on real
        # cutouts). Display-only — array_blob keeps aligned MJy/sr.
        display, bg_level = _background_subtract(aligned)
        stretch = self._stretches.get(field_id)
        if stretch is None:
            # Resume path: a restarted build must keep the stretch already
            # stored on this field's frames or the field shows two
            # parameter sets (the D-26 two-stretch signature).
            stored = await self._stored_stretch_params(field_id)
            if stored is not None:
                state = StretchState(fix_n=STRETCH_FIX_N)
                state.seed(stored)
                stretch = state
            else:
                stretch = await asyncio.to_thread(
                    lambda: StretchState(
                        fix_n=STRETCH_FIX_N,
                        provisional=initial_stretch(display),
                    )
                )
            self._stretches[field_id] = stretch
        params_stretch = await asyncio.to_thread(stretch.observe, display)
        png_blob = None
        if params_stretch is not None:
            png_blob = await asyncio.to_thread(_png_bytes, display, params_stretch)

        meta = {
            "release": ref.release,
            "obs_id": ref.obs_id,
            "detector": ref.detector,
            "bandpass": ref.bandpass,
            "mjd": ref.mjd_start,
            "mjd_end": ref.mjd_end,
            "uri": ref.uri,
            "wavelength_um": wavelength_um,
            "bandwidth_um": bandwidth_um,
            "resolving_power": ref.resolving_power,
            "pixel_scale_arcsec": ref.pixel_scale_arcsec,
            "provenance_version": ref.provenance_version,
            "calibration_level": ref.calibration_level,
            "variance_resampled": var_aligned is not None,
            "flags_resampled": True,
            "grid_wcs": target_wcs.to_header_string(),
            "wcs_cards": _header_cards(data.header),
        }
        if params_stretch is not None:
            # Persist the stretch used for png_blob so on-demand renders
            # (candidate cutouts) match the stored frame image.
            meta["stretch"] = {
                "lo": params_stretch.lo,
                "hi": params_stretch.hi,
                "softness": params_stretch.softness,
                "lo_percentile": params_stretch.lo_percentile,
                "hi_percentile": params_stretch.hi_percentile,
                "n_pixels": params_stretch.n_pixels,
            }
        meta["background_level_mjy_sr"] = bg_level
        meta["display"] = "background_subtracted"
        await put_frame(
            self.db,
            field_id,
            ref.frame_ref,
            array_blob=await asyncio.to_thread(
                encode_frame_array, aligned, variance=var_aligned, flags=flags_aligned
            ),
            wavelength_um=wavelength_um,
            mjd=ref.mjd_start,
            detector=ref.detector,
            png_blob=png_blob,
            meta=meta,
        )
        await self._emit(
            field_id,
            "frame",
            {
                "frame_ref": ref.frame_ref,
                "detector": ref.detector,
                "mjd": ref.mjd_start,
                "wavelength_um": wavelength_um,
            },
        )

    async def _stored_stretch_params(self, field_id: str) -> StretchParams | None:
        """First persisted ``meta.stretch`` on this field's frames, if any."""
        for row in await list_frames(self.db, field_id):
            params = StretchParams.from_dict((row.get("meta") or {}).get("stretch"))
            if params is not None:
                return params
        return None

    @staticmethod
    def _extract_arrays(payload: bytes):
        data = extract_cutout(payload)
        wcs = WCS(data.header, relax=True)
        masked = apply_default_mask(data.image, data.flags)
        return data, wcs, masked

    async def _finalize_groups(self, field_id: str) -> None:
        """§9.4 step 4: one 'all' group over stored aligned arrays."""
        rows = await list_frames(self.db, field_id)
        if len(rows) < 2:
            return
        arrays = []
        refs = []
        for row in rows:
            try:
                z = np.load(io.BytesIO(row["array_blob"]))
                arrays.append(z["data"])
                refs.append(row["frame_ref"])
            except Exception:
                continue
        if len(arrays) < 2:
            return
        group_id = "all"
        await put_group(self.db, field_id, group_id, rule="all", meta={"frames": len(refs)})
        for ref in refs:
            await add_group_frame(self.db, field_id, group_id, ref)
        # combined image is derivable on demand; store nothing extra for v1


def _header_cards(header: Any) -> list[list[Any]]:
    """JSON-safe [keyword, value] pairs for rebuilding the frame WCS."""
    cards = []
    for card in header.cards:
        key = str(card.keyword).strip()
        if not key:
            continue
        value = card.value
        if not isinstance(value, (str, int, float, bool)):
            value = str(value)
        cards.append([key, value])
    return cards


def _wavelength_pair(header: Any, wavetable_hdu: Any, x: float, y: float):
    """(wavelength_um, bandwidth_um) at pixel (x, y); None on failure."""
    try:
        tab = WaveTab.from_hdu(wavetable_hdu)
        return (
            float(wavelength_at(header, tab, x, y)),
            float(bandwidth_at(header, tab, x, y)),
        )
    except Exception:
        return None, None


__all__ = ["PipelineCapError", "PipelineError", "PipelineRunner"]
