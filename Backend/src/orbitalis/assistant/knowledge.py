"""Curated knowledge pack loading and retrieval.

Purpose: Load the assistant knowledge pack from ``config/knowledge/`` (D-16).
Inputs: ``config_dir`` (``Settings.config_dir``) and knowledge pack markdown.
Outputs: Concatenated knowledge text for system prompt injection.
Called by: assistant service (AS.04+).
"""

from __future__ import annotations

from pathlib import Path


class KnowledgePackMissingError(FileNotFoundError):
    """``config/knowledge/`` is absent — the assistant must not run ungrounded."""


def load_knowledge_pack(config_dir: Path) -> str:
    """Load and concatenate all knowledge pack files in a deterministic order.

    Files are loaded in a fixed order to ensure consistent token counts and
    prompt structure. Returns the combined text with file separators.

    Raises ``KnowledgePackMissingError`` when the directory or every expected
    file is missing: an empty pack would let the assistant answer ungrounded
    without anyone noticing (AS.04 review).
    """
    knowledge_dir = Path(config_dir) / "knowledge"
    if not knowledge_dir.is_dir():
        raise KnowledgePackMissingError(f"knowledge pack directory missing: {knowledge_dir}")
    # Deterministic order matching D-16 content structure
    filenames = [
        "sdd_glossary.md",
        "honesty_rules.md",
        "starter_questions.md",
    ]
    parts: list[str] = []
    for name in filenames:
        path = knowledge_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            parts.append(f"# {name}\n\n{text}")
    if not parts:
        raise KnowledgePackMissingError(f"no knowledge pack files found in {knowledge_dir}")
    return "\n\n---\n\n".join(parts)


__all__ = ["KnowledgePackMissingError", "load_knowledge_pack"]
