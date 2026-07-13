-- ── Festival year (F1) ───────────────────────────────────────────────────────
-- Add year column so the same festival name can recur across years
ALTER TABLE festivals ADD COLUMN IF NOT EXISTS year INT;

-- Re-link shows away from year=NULL ghost festivals onto the real (year-set) row,
-- then delete the ghosts. Guards against FK violations when migration 001 re-ran
-- and its UPDATE linked shows to a ghost ID before tracking was introduced.
WITH ghost_ids AS (
    SELECT g.id AS ghost_id, r.id AS real_id
    FROM festivals g
    JOIN festivals r
      ON r.user_id = g.user_id AND r.festival_name = g.festival_name AND r.year IS NOT NULL
    WHERE g.year IS NULL
)
UPDATE shows
SET festival_id = ghost_ids.real_id
FROM ghost_ids
WHERE shows.festival_id = ghost_ids.ghost_id;

DELETE FROM festivals
WHERE year IS NULL
  AND (user_id, festival_name) IN (
    SELECT user_id, festival_name FROM festivals WHERE year IS NOT NULL
  );

-- Backfill year from the earliest show date for each festival
UPDATE festivals f
SET year = (
    SELECT EXTRACT(YEAR FROM MIN(s.date))::int
    FROM shows s WHERE s.festival_id = f.id
)
WHERE f.year IS NULL;

-- Drop the old unique constraint and replace with one that includes year
ALTER TABLE festivals DROP CONSTRAINT IF EXISTS festivals_user_id_festival_name_key;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'festivals_user_id_festival_name_year_key'
    ) THEN
        ALTER TABLE festivals ADD CONSTRAINT festivals_user_id_festival_name_year_key
            UNIQUE (user_id, festival_name, year);
    END IF;
END $$;

-- ── Show photos (F7) ─────────────────────────────────────────────────────────
ALTER TABLE shows ADD COLUMN IF NOT EXISTS photo_url TEXT;

-- ── Notifications ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    user_id   INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor_id  INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type      TEXT NOT NULL CHECK (type IN ('like', 'comment', 'follow', 'tag')),
    show_id   INT REFERENCES shows(id) ON DELETE CASCADE,
    festival_id INT REFERENCES festivals(id) ON DELETE CASCADE,
    comment_id  INT REFERENCES show_comments(id) ON DELETE CASCADE,
    is_read   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(user_id, is_read) WHERE is_read = FALSE;
