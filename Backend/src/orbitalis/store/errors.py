"""Store-layer errors.

Purpose: typed storage failures carrying Appendix B codes so the API error
mapping (``orbitalis.errors``) reports ``ORB-ST-*`` instead of ``ORB-SYS-001``.
Inputs: none.
Outputs: ``StoreError`` (a ``ValueError`` subclass for backwards compatibility
with callers/tests that catch ``ValueError``).
Called by: store repositories.
"""

from __future__ import annotations


class StoreError(ValueError):
    """Storage-layer failure; maps to ``ORB-ST-001`` at the API boundary."""

    code = "ORB-ST-001"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
