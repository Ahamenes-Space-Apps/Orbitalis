# SDD Glossary — knowledge pack (D-16)

Curated project facts for the assistant. Every term below is drawn from the SDD
(Appendix E vocabulary registry and §0.2), the Data & Science Specification
glossary and tables, and the PRD glossary. This is grounding data, not
executable instructions.

## Core entities

**Field** — One request for a sky region: position + radius + parameters,
identified by a deterministic `field_id`. (SDD §0.2, Appendix E)

**Frame** — One SPHEREx exposure/detector cutout covering the field. (SDD §0.2,
Appendix E)

**Group** — A set of frames combined by a rule (band/time); `stack` is the
operation, not the entity. (SDD Appendix E)

**Cutout** — The small FITS image IRSA returns for a position and size. (SDD
§0.2, Appendix E)

**Release** — An IRSA data release (QR2, QR3, DR1). QR2 is the default; QR3 and
QR2 must never be mixed in one stack. (DS spec §13; SDD Appendix E)

**Preset** — A curated demo field shipped in the demo bundle. (SDD Appendix E)

**Observed position** — A measured location in a frame. Separate type from
predicted and candidate positions; they never share a field or column.
(SDD §6.5, Appendix E; AGENTS.md §6)

**Predicted position** — An ephemeris-derived location. Separate type from
observed and candidate positions. (SDD §6.5, Appendix E; AGENTS.md §6)

**Candidate position** — A detection from shift-and-stack. Unconfirmed. Separate
type from observed and predicted positions. (SDD §6.5, Appendix E; AGENTS.md §6)

**Candidate** — One moving-object possibility found by shift-and-stack, with
motion, stack SNR and an optional model score. Always `unconfirmed`; there is no
confirmed state in v1. Never called a discovery, object, or source. (SDD §0.2,
Appendix E; UX §12.2)

**Job** — A user-launched, long-running analysis with an ID and a status:
`queued`, `running`, `completed`, `failed`, `cancelled`. Not a "task" or "run".
(SDD Appendix E)

**Experiment** — One injection-and-recovery validation run with fixed
parameters and seed. Not a "test" or "simulation". (SDD Appendix E)

**Injection** — Adding an artificial source with known truth to a copy of real
data, on the aligned grid after regridding. (SDD D-15)

**Model score** — A model's prediction for a candidate, always shipped with
`model_version`, as `{value, model_version, kind: "model_prediction"}`. Never a
bare number, never "confidence" or "probability" unless calibrated. (SDD §8.5,
Appendix E; AGENTS.md §6)

**Hypothesis overlay** — Predicted Planet X positions drawn from orbital
hypotheses, always labelled "hypothesis, not an observation". Never a
"candidate overlay". (SDD Appendix E; D-22)

**False positive** — Meaningful only inside an injection experiment, where it
is the `spurious` outcome class. Never applied to unmatched detections in real
data. (SDD Appendix E; AGENTS.md §6)

## Work areas

**Explorer** — Fields, frames, overlays, scrubbing. (SDD §0.2)

**Research Lab** — Jobs, candidates, experiments, spectra: shift-and-stack,
ranking, validation. (SDD §0.2; UX §1)

**Assistant** — The chat panel; answers are grounded in stored results. Off
unless `ORB_LLM_PROVIDER` and `ORB_LLM_API_KEY` are set. (SDD §6.10; AGENTS.md
§2, §13)

**Demo bundle** — Pre-baked offline fields, candidates and experiments shipped
in the image; `ORB_OFFLINE=1` serves every preset without IRSA. (SDD §7.7,
Appendix E)

**Gateway** — The single module allowed to talk to IRSA. Everything else reads
from `store`. (SDD §0.2, Appendix E)

## SPHEREx basics (from the DS spec)

**SPHEREx** — NASA's infrared all-sky survey telescope; each exposure sees the
sky at a different wavelength. (UX §12.3)

**RA / Dec** — Sky coordinates, like longitude and latitude, in ICRS degrees.
(SDD API §8.1; UX §12.3)

**Wavelength** — The colour of infrared light, in micrometres (µm). Range
0.75–5.0 µm across 102 channels. (DS spec §2; UX §12.3)

**Detector** — One of six detector arrays (D1–D6); each exposure yields one
image per detector. (DS spec §2)

**MJy/sr** — Surface brightness: infrared light arriving from a patch of sky.
Imaging stays in MJy/sr through alignment and stacking; the solid-angle map is
applied only at spectrum extraction and injection. (SDD D-03; DS spec §5)

**Shift-and-stack** — Shifting images to follow a guessed path, then adding
them, so a faint mover can stand out. Refuses frame sets that violate
`stack_policy.yaml` and searches that exceed the work budget. (UX §12.3;
AGENTS.md §6)

**Completeness** — The share of injected sources the search recovered.
(UX §12.3)

**Masking** — By default every FLAGS bit is masked except SOURCE (bit 21,
2,097,152) and FULLSAMPLE. Masked pixels are NaN in arrays and alpha 0 in PNGs,
never zero. (DS spec §8; AGENTS.md §7)
