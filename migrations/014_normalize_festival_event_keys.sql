-- Festival events were originally keyed as name|year|city, causing the same festival
-- to appear as separate events when users spelled the city differently.
-- Rekey to name|year only, merging any duplicates that result.

-- Step 1: For each group of festival events sharing the same name|year (after dropping city),
-- pick the earliest-created one as canonical. Re-link all festivals to the canonical event.
WITH canonical AS (
    SELECT DISTINCT ON (LOWER(artist) || '|' || EXTRACT(YEAR FROM date)::TEXT)
        id AS canonical_id,
        LOWER(artist) || '|' || EXTRACT(YEAR FROM date)::TEXT AS new_key
    FROM events
    WHERE event_type = 'festival'
    ORDER BY LOWER(artist) || '|' || EXTRACT(YEAR FROM date)::TEXT, id ASC
)
UPDATE festivals f
SET event_id = c.canonical_id
FROM events e
JOIN canonical c
  ON LOWER(e.artist) || '|' || EXTRACT(YEAR FROM e.date)::TEXT = c.new_key
WHERE f.event_id = e.id
  AND f.event_id IS DISTINCT FROM c.canonical_id;

-- Step 2: Delete now-orphaned festival events (those that lost all their festival links)
DELETE FROM events
WHERE event_type = 'festival'
  AND id NOT IN (SELECT DISTINCT event_id FROM festivals WHERE event_id IS NOT NULL);

-- Step 3: Update normalized_key on surviving events to the new name|year format
UPDATE events
SET normalized_key = LOWER(artist) || '|' || EXTRACT(YEAR FROM date)::TEXT
WHERE event_type = 'festival'
  AND normalized_key != LOWER(artist) || '|' || EXTRACT(YEAR FROM date)::TEXT;

-- Step 4: Link any festivals that still have no event_id
WITH festival_dates AS (
    SELECT
        f.id AS festival_id,
        LOWER(f.festival_name) || '|' || EXTRACT(YEAR FROM MIN(s.date))::TEXT AS norm_key,
        f.festival_name,
        MIN(s.date) AS first_date,
        f.city
    FROM festivals f
    JOIN shows s ON s.festival_id = f.id
    WHERE f.event_id IS NULL AND f.festival_name IS NOT NULL
    GROUP BY f.id, f.festival_name, f.city
),
inserted AS (
    INSERT INTO events (normalized_key, artist, date, venue, city, event_type)
    SELECT DISTINCT ON (norm_key) norm_key, festival_name, first_date, festival_name, city, 'festival'
    FROM festival_dates
    ORDER BY norm_key
    ON CONFLICT (normalized_key) DO NOTHING
    RETURNING id, normalized_key
)
UPDATE festivals f
SET event_id = COALESCE(ins.id, e.id)
FROM festival_dates fd
LEFT JOIN inserted ins ON ins.normalized_key = fd.norm_key
LEFT JOIN events e ON e.normalized_key = fd.norm_key
WHERE f.id = fd.festival_id AND f.event_id IS NULL;
