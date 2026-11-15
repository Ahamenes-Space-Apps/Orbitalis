"""Overlay providers: known objects and Planet X hypothesis regions.

Purpose: Public surface for overlay sources. ``known`` objects come from
curated preset config (api layer); ``hypothesis`` regions come from
``config/hypothesis.yaml`` via :mod:`orbitalis.overlays.hypothesis`.
Inputs: config directories and sky positions.
Outputs: region dicts, containment tests.
Called by: ``api.overlays``.
"""

from orbitalis.overlays.hypothesis import contains_point, load_hypothesis, shape_center

__all__ = ["contains_point", "load_hypothesis", "shape_center"]
