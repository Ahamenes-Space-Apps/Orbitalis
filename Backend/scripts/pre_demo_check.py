"""OPS.06: automated pre-demo rehearsal (SDD §15.4 items 1, 4, 7, 8).

Purpose: boot the real app in ``ORB_OFFLINE=1`` mode against the committed
``demo_bundle/`` and verify the demo path end to end — every preset attaches
to its baked field, frames render (stored or on-demand), candidates stay
``unconfirmed``, experiments carry the four outcome classes, and
``/capabilities`` reports honest feature state. Items that need live infra
(deploy, CORS, min-instances, provider spend) remain manual checklist rows
in SDD §15.4.

Usage::

    uv run python scripts/pre_demo_check.py

Exit code 0 = all checks pass.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from orbitalis.main import create_app
from orbitalis.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        settings = Settings(
            offline=True,
            data_dir=Path(td),
            demo_bundle_dir=ROOT / "demo_bundle",
            config_dir=ROOT / "config",
        )
        app = create_app(settings)
        with TestClient(app) as c:
            # §15.4.4: ORB_OFFLINE=1 rehearsal — IRSA must report offline.
            r = c.get("/api/v1/health")
            check(
                "health offline", r.status_code == 200 and r.json()["data"]["status"] == "offline"
            )

            # §15.4.7: capabilities report honest state (no LLM key here).
            r = c.get("/api/v1/capabilities")
            caps = r.json()["data"] if r.status_code == 200 else {}
            check("capabilities 200", r.status_code == 200)
            assistant = (caps.get("features") or {}).get("assistant") or caps.get("assistant") or {}
            check(
                "assistant honestly disabled",
                assistant.get("enabled") is False,
                str(assistant.get("reason", "")),
            )

            presets = c.get("/api/v1/presets").json()["data"]["presets"]
            check("presets listed", len(presets) >= 6, f"{len(presets)} presets")

            for p in presets:
                name = p["name"]
                r = c.post("/api/v1/fields", json={"preset": name})
                if r.status_code != 200:
                    check(f"{name}: attach", False, r.text[:120])
                    continue
                fid = r.json()["data"]["field_id"]
                frames = c.get(f"/api/v1/fields/{fid}/frames").json()["data"]["frames"]
                check(f"{name}: frames", len(frames) > 0, f"{len(frames)} frames")
                if not frames:
                    continue
                ref = frames[0]["frame_ref"]
                png = c.get(f"/api/v1/fields/{fid}/frames/{ref}.png")
                check(
                    f"{name}: frame png",
                    png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n",
                    f"status={png.status_code}",
                )
                meta = frames[0].get("meta") or {}
                check(
                    f"{name}: display label",
                    meta.get("display") == "background_subtracted",
                    str(meta.get("display")),
                )
                # §15.4.8 shape: candidates unconfirmed, experiments have metrics
                cands = c.get(f"/api/v1/fields/{fid}/candidates").json()["data"]["candidates"]
                bad = [x["candidate_id"] for x in cands if x.get("status") != "unconfirmed"]
                check(f"{name}: candidates unconfirmed", not bad, f"{len(cands)} rows")
                exps = c.get(f"/api/v1/fields/{fid}/experiments").json()["data"]["experiments"]
                # four outcome classes (VAL.02): recovered / missed /
                # preexisting truth + spurious detections
                need = {
                    "n_recovered_truth",
                    "n_missed_truth",
                    "n_preexisting_truth",
                    "n_spurious_detections",
                }
                ok_exp = all(need <= set(e.get("metrics") or {}) for e in exps)
                check(f"{name}: experiment outcome classes", bool(exps) and ok_exp)

    print()
    if FAILURES:
        print(f"PRE-DEMO CHECK FAILED: {len(FAILURES)} check(s)")
        return 1
    print("PRE-DEMO CHECK PASSED — offline bundle serves every preset end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
