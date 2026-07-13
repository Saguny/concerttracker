CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    normalized_key TEXT NOT NULL UNIQUE,
    artist TEXT NOT NULL,
    date DATE NOT NULL,
    venue TEXT,
    city TEXT,
    event_type TEXT NOT NULL DEFAULT 'unverified',
    setlistfm_id TEXT,
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_events_artist ON events (LOWER(artist));
CREATE INDEX IF NOT EXISTS idx_events_date ON events (date);

ALTER TABLE shows ADD COLUMN IF NOT EXISTS event_id INT REFERENCES events(id);
CREATE INDEX IF NOT EXISTS idx_shows_event_id ON shows (event_id) WHERE event_id IS NOT NULL;
