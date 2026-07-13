-- Clean up ghost festival rows created by migration 001 re-running before migration tracking
-- was introduced. Re-links shows to the real festival row first to avoid FK violations.
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

-- Backfill year for any festivals that still have year=NULL (migration 002's UPDATE
-- may have been interrupted before completing on the first crash).
UPDATE festivals f
SET year = (
    SELECT EXTRACT(YEAR FROM MIN(s.date))::int
    FROM shows s WHERE s.festival_id = f.id
)
WHERE f.year IS NULL;
