"""Assistant availability helpers (AS.05 degradation).

Purpose: Determine whether the assistant is configured and may be exposed,
with an honest reason when it is not.
Inputs: Application settings (``ORB_LLM_*``, ``ORB_ASSISTANT_*``).
Outputs: ``(ready, reason)`` — reason is ``None`` when ready, else a short
machine-stable string safe to show in ``/capabilities``.
Called by: API meta/capability reporting and assistant request gating.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orbitalis.settings import Settings


def is_assistant_configured(settings: Settings) -> bool:
    """Return True only when required assistant settings are present and non-empty."""
    provider = settings.llm_provider
    api_key = settings.llm_api_key
    return bool(provider and provider.strip() and api_key and api_key.strip())


def assistant_readiness(settings: Settings) -> tuple[bool, str | None]:
    """Full readiness check for capability reporting and request gating.

    Ready requires: provider + API key + model + a resolvable base URL +
    a loadable knowledge pack. Each failure maps to a specific reason so
    ``/capabilities`` never claims "not implemented" for a working feature
    nor "enabled" for a broken one (AS.05 review).
    """
    provider = (settings.llm_provider or "").strip()
    api_key = (settings.llm_api_key or "").strip()
    if not provider or not api_key:
        return False, "not configured"
    if not (settings.llm_model or "").strip():
        return False, "model not set"
    # Base URL must resolve: explicit override or a known provider mapping.
    from orbitalis.assistant.providers.openai_compatible import PROVIDER_BASE_URLS

    if not settings.llm_base_url and provider.lower() not in PROVIDER_BASE_URLS:
        return False, "unknown provider"
    # The knowledge pack grounds every answer; a missing pack means the
    # assistant would run ungrounded (AS.04 review).
    try:
        from orbitalis.assistant.knowledge import load_knowledge_pack

        load_knowledge_pack(settings.config_dir)
    except Exception:
        return False, "knowledge pack missing"
    return True, None


__all__ = ["assistant_readiness", "is_assistant_configured"]
