# Assistant starter questions — UX-8

Exactly five starter questions for the assistant dock, written by the ML
engineer. Each is grounded in a documented project capability (shift-and-stack
search, model ranking, injection-and-recovery validation, flag masking, and the
observed/predicted/candidate position distinction). The assistant footnote
"AI answer based on this field's stored results. Candidates are unconfirmed."
applies to every answer. (SDD D-16; UX-8; UX §7.1 flow F5)

1. **What does this candidate's stack SNR mean, and is it confirmed?**
   Grounding: shift-and-stack search (UX §12.3), stack SNR tooltip (UX §12.1),
   candidates are always `unconfirmed` (SDD §8.5).

2. **How should I read the model score next to this candidate?**
   Grounding: model score is a prediction with `model_version`, never confidence
   or probability (SDD §8.5; AGENTS.md §6).

3. **How do I know how well the search actually works?**
   Grounding: injection-and-recovery validation, outcome classes
   `recovered`/`missed`/`preexisting`/`spurious`, precision relative to injected
   sources; answers must state the experiment's field, seed and n and never
   generalize recall beyond that experiment (SDD D-15; UX §12.1).

4. **Which pixels are masked, and what do the flag bits mean?**
   Grounding: default mask every FLAGS bit except SOURCE (bit 21) and FULLSAMPLE;
   masked pixels are NaN / alpha 0 (DS spec §8; AGENTS.md §7).

5. **What is the difference between observed, predicted and candidate positions?**
   Grounding: three separate position types that never share a field or column;
   candidate positions are unconfirmed detections (SDD §6.5; AGENTS.md §6).
