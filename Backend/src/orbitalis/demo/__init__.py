"""Demo bundle loader and exporter (SDD §6.7, §7.7).

Purpose: Public surface for the shipped demo bundle: ``load_bundle`` inserts
pre-baked preset fields (frames, jobs, candidates, experiments) at startup;
``export_field`` writes the same layout for ``scripts/bake_demo.py``.
Inputs: ``Database`` handle and bundle directory.
Outputs: load counts / written bundle files.
Called by: ``main._build_services``, ``scripts/bake_demo.py``, tests.
"""

from orbitalis.demo.bundle import export_field, load_bundle

__all__ = ["export_field", "load_bundle"]
