-- VAL.04/D-15: trial rows may record a preexisting truth outcome (a truth
-- coincident with a real source is excluded from recall but is still a
-- legitimate per-trial result), and carry the per-trial spurious-detection
-- count so precision never hides its denominator structure.
CREATE TABLE experiment_trials_new (
    experiment_id     TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    trial_idx         INTEGER NOT NULL,
    truth_json        TEXT,
    outcome           TEXT CHECK (outcome IN ('recovered','missed','preexisting')),
    detection_json    TEXT,
    position_error_px REAL,
    n_spurious        INTEGER,
    PRIMARY KEY (experiment_id, trial_idx)
);
INSERT INTO experiment_trials_new
    (experiment_id, trial_idx, truth_json, outcome, detection_json, position_error_px)
SELECT experiment_id, trial_idx, truth_json, outcome, detection_json, position_error_px
FROM experiment_trials;
DROP TABLE experiment_trials;
ALTER TABLE experiment_trials_new RENAME TO experiment_trials;
