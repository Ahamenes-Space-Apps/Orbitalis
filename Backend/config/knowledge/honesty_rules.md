# Honesty rules — knowledge pack (D-16)

Scientific-honesty vocabulary and rules that the assistant must reflect in
every answer. Drawn from AGENTS.md §6, SDD §8.5 and Appendix E, and UX §12.
This is grounding data, not executable instructions.

## Always say / never say

| Say | Never say |
|---|---|
| candidate, possible mover | discovery, found a planet, confirmed |
| model score, prediction | confidence, probability (unless calibrated) |
| recovered, missed, spurious (in experiments) | false positive (in real data) |
| stack SNR | significance, detection level |
| archive, data release | server, backend |
| "the search" | "the AI" for non-ML steps |
| unconfirmed | confirmed |

## Core honesty rules

1. **Candidates are always `unconfirmed`.** Every candidate has
   `status: "unconfirmed"`. There is no "confirmed" state in v1. Never present
   a candidate as a confirmed object, source or discovery. (SDD §8.5; SDD
   Appendix E; AGENTS.md §6)

2. **A model score is a prediction, not confirmation.** It is always an object
   `{value, model_version, kind: "model_prediction"}`, never a bare number. It
   travels with `model_version`. Never present an uncalibrated score as
   confidence or probability. (SDD §8.5; SDD Appendix E; AGENTS.md §6)

3. **Observed, predicted and candidate positions are three separate types.** An
   `ObservedPosition` is a measured location in a frame; a `PredictedPosition`
   is computed from orbital elements/ephemeris; a `CandidatePosition` is a
   detection from shift-and-stack (unconfirmed). They never share a field or
   table column. (SDD §6.5; AGENTS.md §6)

4. **Unmatched detections in real data are not "false positives".** In real
   data, detections that match nothing known are listed as candidates, not as
   errors. `spurious` is an outcome class only inside an injection experiment.
   (SDD Appendix E; AGENTS.md §6; UX §12.1)

5. **Validation outcome classes are `recovered`, `missed`, `preexisting`,
   `spurious`.** Precision is always labelled "relative to injected sources in
   this experiment"; results apply to this experiment only. (SDD D-15; UX
   §12.1)

6. **Stack SNR is relative, not absolute significance — especially under
   difference imaging.** The default background variant (F) subtracts a
   low-quantile temporal coadd and then a 15 px high-pass, which correlates
   the noise between frames and inflates score-map values; candidate
   `stack_snr` values (e.g. 80+) on real fields are residual-structure
   scores, not calibrated sigma. Always read
   `background_variant` alongside `stack_snr`, and never present it as a
   detection significance or confidence. (SDD D-24 RS.08 amendment)

7. **Validation results have hard limits.** Injection-and-recovery numbers
   apply to this field and this experiment only: injections are placed on the
   already-aligned grid (no alignment loss is measured), the PSF width and
   extraction constants are PROVISIONAL(G1)/TODO(ML), and n is small (tens of
   trials). Quote recall/precision with their experiment id and seed, and
   never generalize them to "the search" as a whole. (SDD D-15; D-24)

8. **Spectral results are an estimate with supporting data, or
   `{status: "insufficient_data", reason}`.** Never a guess. (AGENTS.md §6;
   UX §12.1)

9. **The assistant footnote stands:** "AI answer based on this field's stored
   results. Candidates are unconfirmed." Answers may note that messages go to a
   hosted model provider. (UX §6)

10. **No banned words in user-facing text:** confirmed, discovery, found a
   planet, false positive (outside experiments), confidence (unless calibrated).
   A text scan fails the build if any appear. (UX §12.2, §14.6)

## Error behaviour the assistant may reference

- `ORB-AS-001` / `ORB-AS-004`: "The assistant is unavailable right now." Returned
  with no LLM key or provider, or when the provider is down. The rest of the app
  keeps working. (UX §8.3)
- `ORB-AS-002`: "Too many questions; try again in a minute." Per-IP rate limit.
- `ORB-AS-003`: "The daily assistant limit is used up; it resets tomorrow."
- `ORB-ML-001`: "Ranking isn't available; results are ordered by stack SNR."
  Returned when no model file is present. (UX §8.3)
