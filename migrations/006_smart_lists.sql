ALTER TABLE lists ADD COLUMN IF NOT EXISTS list_type TEXT NOT NULL DEFAULT 'curated'
    CHECK (list_type IN ('curated', 'smart'));
ALTER TABLE lists ADD COLUMN IF NOT EXISTS smart_filter JSONB;
