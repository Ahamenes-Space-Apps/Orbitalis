"""HTTP layer public surface.

Purpose: Package marker for the /api/v1 routers and shared helpers.
Inputs: None at import.
Outputs: submodules (fields, jobs, meta, schemas, deps, stream, assistant).
Called by: orbitalis.main (mounts api.router.api_router).
"""

from orbitalis.api import assistant  # noqa: F401
