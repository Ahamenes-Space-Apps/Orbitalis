-- PL.01 review: persist the deterministic common-grid WCS per field.
ALTER TABLE fields ADD COLUMN grid_json TEXT;
