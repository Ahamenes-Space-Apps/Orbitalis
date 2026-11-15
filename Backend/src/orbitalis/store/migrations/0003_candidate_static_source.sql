-- RS.05/D-24: top-level static-source flag on candidates.
-- 1 = flagged as likely stationary by the provisional heuristic; 0 = evaluated,
-- not flagged; NULL = predates the flag (unknown).
ALTER TABLE candidates ADD COLUMN static_source INTEGER;
