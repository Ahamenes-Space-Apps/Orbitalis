"""Stack-compatibility policy loaded from stack_policy.yaml (RS.04, D-14).

Purpose: Validate whether a supplied frame set is combinable for shift-and-stack.
Inputs: Frame records/rows and stack policy configuration.
Outputs: PolicyResult or ORB-RS-002 rejection with per-frame reasons.
Called by: research.moving_object_search (and future research handlers).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from orbitalis.errors import APIError

# PROVISIONAL(G1): D-14 policy values are provisional pending ML measurement.
_REQUIRED_KEYS = (
    "same_release",
    "same_detector",
    "epoch_gap_days",
    "max_time_span_days",
    "max_wavelength_span_um",
)


@dataclass(frozen=True)
class PolicyResult:
    """Result of stack-compatibility evaluation for a supplied frame set."""

    combinable: bool
    reasons: list[dict[str, Any]] = field(default_factory=list)


def _frame_ref(frame: Mapping[str, Any]) -> str:
    ref = frame.get("frame_ref")
    return str(ref) if ref is not None else "<unknown>"


def _meta(frame: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = frame.get("meta")
    return meta if isinstance(meta, dict) else {}


def _get_release(frame: Mapping[str, Any]) -> str | None:
    val = _meta(frame).get("release", frame.get("release"))
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _get_detector(frame: Mapping[str, Any]) -> int | str | None:
    val = frame.get("detector")
    if val is None:
        val = _meta(frame).get("detector")
    return val


def _get_mjd(frame: Mapping[str, Any]) -> float | None:
    val = frame.get("mjd")
    if val is None:
        val = _meta(frame).get("mjd")
    if val is None:
        return None
    try:
        out = float(val)
    except Exception:
        return None
    return out


def _get_wavelength(frame: Mapping[str, Any]) -> float | None:
    val = frame.get("wavelength_um")
    if val is None:
        val = _meta(frame).get("wavelength_um")
    if val is None:
        return None
    try:
        out = float(val)
    except Exception:
        return None
    return out


def load_stack_policy(path: str | Path) -> dict[str, Any]:
    """Load and validate stack policy YAML configuration."""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        raise APIError(
            "ORB-SYS-001",
            "stack policy configuration is missing",
            detail={"path": str(p)},
        )
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise APIError(
            "ORB-SYS-001",
            "stack policy configuration is invalid",
            detail={"path": str(p)},
        ) from exc
    if not isinstance(data, dict):
        raise APIError(
            "ORB-SYS-001",
            "stack policy configuration is invalid",
            detail={"path": str(p)},
        )

    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise APIError(
            "ORB-SYS-001",
            "stack policy configuration is invalid",
            detail={"path": str(p), "missing": missing},
        )

    for key in ("same_release", "same_detector"):
        if not isinstance(data.get(key), bool):
            raise APIError(
                "ORB-SYS-001",
                "stack policy configuration is invalid",
                detail={"path": str(p), "key": key},
            )

    for key in ("epoch_gap_days", "max_time_span_days", "max_wavelength_span_um"):
        val = data.get(key)
        if val is None or isinstance(val, bool):
            raise APIError(
                "ORB-SYS-001",
                "stack policy configuration is invalid",
                detail={"path": str(p), "key": key},
            )
        try:
            num = float(val)
        except Exception as exc:
            raise APIError(
                "ORB-SYS-001",
                "stack policy configuration is invalid",
                detail={"path": str(p), "key": key},
            ) from exc
        if not (num > 0):
            raise APIError(
                "ORB-SYS-001",
                "stack policy configuration is invalid",
                detail={"path": str(p), "key": key},
            )

    return dict(data)


def check_combinable(
    frames: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None = None,
    *,
    policy_path: str | Path | None = None,
) -> PolicyResult:
    """Check whether the supplied frame set is combinable under RS.04/D-14.

    RS.04 judges combinability of the supplied set; it does not select a search window.
    Returns PolicyResult with one clear reason per offending frame.
    """
    if policy is None:
        if policy_path is None:
            raise APIError("ORB-SYS-001", "stack policy configuration is missing")
        policy = load_stack_policy(policy_path)
    cfg = dict(policy)

    reasons_by_ref: dict[str, list[str]] = {}

    def mark(frame: Mapping[str, Any], reason: str) -> None:
        ref = _frame_ref(frame)
        reasons_by_ref.setdefault(ref, [])
        if reason not in reasons_by_ref[ref]:
            reasons_by_ref[ref].append(reason)

    if not frames:
        return PolicyResult(combinable=True, reasons=[])

    # Missing/unknown metadata is explicit (never silently compatible).
    for fr in frames:
        if _get_release(fr) is None:
            mark(fr, "missing_release")
        if cfg.get("same_detector") and _get_detector(fr) is None:
            mark(fr, "missing_detector")
        if _get_mjd(fr) is None:
            mark(fr, "missing_mjd")
        if _get_wavelength(fr) is None:
            mark(fr, "missing_wavelength_um")

    # Same-release compatibility.
    if cfg.get("same_release", True):
        known_releases = [r for r in (_get_release(fr) for fr in frames) if r is not None]
        if known_releases:
            canonical = known_releases[0]
            for fr in frames:
                rel = _get_release(fr)
                if rel is None or rel != canonical:
                    mark(fr, "release_mismatch")
        else:
            for fr in frames:
                mark(fr, "release_mismatch")

    # Same-detector compatibility only when configured.
    if cfg.get("same_detector", False):
        known_detectors = [d for d in (_get_detector(fr) for fr in frames) if d is not None]
        if known_detectors:
            canonical_det = known_detectors[0]
            for fr in frames:
                det = _get_detector(fr)
                if det is None or det != canonical_det:
                    mark(fr, "detector_mismatch")
        else:
            for fr in frames:
                mark(fr, "detector_mismatch")

    # Epoch and time-span checks (MJD days).
    timed = [(fr, _get_mjd(fr)) for fr in frames]
    valid_timed = [(fr, t) for fr, t in timed if t is not None]
    if valid_timed:
        by_time = sorted(valid_timed, key=lambda x: (x[1], _frame_ref(x[0])))
        epoch_gap = float(cfg["epoch_gap_days"])
        epochs: list[list[tuple[Mapping[str, Any], float]]] = [[by_time[0]]]
        for prev, cur in zip(by_time, by_time[1:], strict=False):
            gap = cur[1] - prev[1]
            if gap > epoch_gap:
                epochs.append([cur])
                mark(cur[0], f"epoch_gap_exceeded:{gap:.6g}d")
            else:
                epochs[-1].append(cur)

        if len(epochs) > 1:
            primary_ids = {id(x[0]) for x in epochs[0]}
            for ep in epochs[1:]:
                for fr, _ in ep:
                    if id(fr) not in primary_ids:
                        mark(fr, "multiple_epochs_in_set")

        times = [t for _, t in by_time]
        span = max(times) - min(times)
        max_span = float(cfg["max_time_span_days"])
        if span > max_span:
            t0 = min(times)
            for fr, t in by_time:
                if t - t0 > max_span:
                    mark(fr, f"time_span_exceeded:{span:.6g}d")

    # Wavelength span check.
    wl_valid = [(fr, _get_wavelength(fr)) for fr in frames]
    wl_valid = [(fr, w) for fr, w in wl_valid if w is not None]
    if wl_valid:
        wls = [w for _, w in wl_valid]
        wspan = max(wls) - min(wls)
        max_wspan = float(cfg["max_wavelength_span_um"])
        if wspan > max_wspan:
            wmin = min(wls)
            for fr, w in wl_valid:
                if w - wmin > max_wspan:
                    mark(fr, f"wavelength_span_exceeded:{wspan:.6g}um")

    reasons = [
        {"frame_ref": ref, "reasons": rs}
        for ref, rs in sorted(reasons_by_ref.items(), key=lambda kv: kv[0])
    ]
    return PolicyResult(combinable=len(reasons) == 0, reasons=reasons)


def enforce_combinable(
    frames: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None = None,
    *,
    policy_path: str | Path | None = None,
) -> PolicyResult:
    """Raise ORB-RS-002 if supplied frames violate the stack policy."""
    result = check_combinable(frames, policy, policy_path=policy_path)
    if not result.combinable:
        raise APIError(
            "ORB-RS-002",
            "selected frames violate stack policy",
            detail={
                "reasons": result.reasons,
                "hint": "pass frame_refs to select a compatible subset of frames",
            },
        )
    return result


__all__ = ["PolicyResult", "check_combinable", "enforce_combinable", "load_stack_policy"]
