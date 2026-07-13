-- headliners array: promote single artist to list; backward-compatible
ALTER TABLE shows ADD COLUMN IF NOT EXISTS headliners TEXT[] NOT NULL DEFAULT '{}';
UPDATE shows SET headliners = ARRAY[artist]
    WHERE headliners = '{}' AND artist IS NOT NULL AND artist != '';

-- city trim backfill
UPDATE shows SET city = TRIM(city) WHERE city IS NOT NULL AND city != TRIM(city);

-- pg_trgm for fast prefix/substring user + artist discovery search
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_users_username_trgm ON users USING GIN (username gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_artists_name_trgm   ON artists USING GIN (name gin_trgm_ops);

-- index shows.headliners for artist-appearance smart list queries
CREATE INDEX IF NOT EXISTS idx_shows_headliners ON shows USING GIN (headliners);
