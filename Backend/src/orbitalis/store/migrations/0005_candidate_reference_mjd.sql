-- X-13: store the search's own reference epoch on each candidate instead of
-- deriving it as the earliest frame MJD at read time.
ALTER TABLE candidates ADD COLUMN reference_mjd REAL;
