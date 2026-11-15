"""Env-driven settings via pydantic-settings.

Purpose: Single settings object (SDD §10.2); every default comes from
Appendix A so nothing is a bare literal in module code.
Inputs: Process environment (``ORB_*`` variables).
Outputs: ``Settings`` instance consumed by the composition root.
Called by: ``main.create_app``; tests may override fields directly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """ORBITALIS runtime configuration. See SDD Appendix A."""

    model_config = SettingsConfigDict(env_prefix="ORB_", extra="ignore")

    env: str = "dev"
    port: int = 8080
    data_dir: Path = Path(".data")
    cache_max_mb: int = 400
    # Relative paths resolve against the process CWD (repo root locally,
    # /app in the image). Do NOT derive these from __file__: the Docker
    # image installs non-editable, so __file__ points into site-packages.
    config_dir: Path = Path("config")
    demo_bundle_dir: Path = Path("demo_bundle")
    offline: bool = False
    cors_origins: str = "http://localhost:3000"
    log_level: str = "INFO"

    irsa_concurrency: int = 4
    irsa_tap_timeout_s: float = 60.0
    irsa_tap_url: str = "https://irsa.ipac.caltech.edu/TAP/sync"
    irsa_cutout_timeout_s: float = 60.0
    # names match what gateway/limiter.py actually reads (Appendix A drift
    # is tracked as a roadmap follow-up)
    irsa_breaker_threshold: int = 5
    irsa_breaker_cooldown_s: float = 30.0
    tap_ttl_s: int = 21600

    max_frames: int = 120
    max_radius_deg: float = 0.5
    max_active_fields: int = 3
    grace_s: float = 20.0
    stretch_fix_n: int = 8

    jobs_max_running: int = 1
    jobs_queue_max: int = 10
    job_timeout_s: float = 600.0

    search_max_work: float = 3e8
    stack_policy: str = "config/stack_policy.yaml"
    model_path: str = "models/candidate_cnn.onnx"

    # VAL.04 / D-15: initial match radius 1.5 px (floor 1 px); recalibrate to
    # 3x the median position error of recovered high-SNR sources after the
    # first 100-injection run.
    match_radius_px: float = 1.5
    validation_max_injections: int = 200

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    # Optional override for the provider base URL; when unset the provider
    # name selects the endpoint (see PROVIDER_BASE_URLS in the adapter).
    llm_base_url: str | None = None
    # Ordered fallback providers, JSON list of
    # {"api_key", "base_url", "model"} — tried only while a stream has
    # emitted nothing (rate limits, outages). All targets must speak the
    # OpenAI chat-completions SSE dialect.
    llm_fallbacks: str | None = None
    assistant_rpm: int = 10
    assistant_daily_cap: int | None = None
    assistant_max_tool_iters: int = 5
    assistant_max_output_tokens: int = 800

    @model_validator(mode="after")
    def _resolve_config_dir(self) -> Settings:
        # Same fallback as demo_bundle_dir in main.py: prefer the
        # CWD-relative path (repo root locally, /app in the image), else
        # derive from the package location (src-layout checkouts only).
        if not self.config_dir.is_absolute():
            cwd_candidate = Path.cwd() / self.config_dir
            self.config_dir = (
                cwd_candidate
                if cwd_candidate.exists()
                else Path(__file__).resolve().parents[2] / self.config_dir
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "orbitalis.db"


__all__ = ["Settings"]
