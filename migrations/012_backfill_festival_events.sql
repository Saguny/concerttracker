-- Create events for existing festivals (keyed by festival_name + year + city)
-- Create one event per (festival_name, year) — city excluded from key since users spell it differently
WITH festival_dates AS (
    SELECT
        f.id AS festival_id,
        f.festival_name,
        f.city,
        MIN(s.date) AS first_date,
        EXTRACT(YEAR FROM MIN(s.date))::TEXT AS year_str
    FROM festivals f
    JOIN shows s ON s.festival_id = f.id
    WHERE f.festival_name IS NOT NULL AND f.festival_name != ''
      AND f.event_id IS NULL
    GROUP BY f.id, f.festival_name, f.city
),
inserted AS (
    INSERT INTO events (normalized_key, artist, date, venue, city, event_type)
    SELECT DISTINCT ON (norm_key)
        norm_key,
        festival_name,
        first_date,
        festival_name,
        city,
        'festival'
    FROM (
        SELECT
            LOWER(festival_name) || '|' || year_str AS norm_key,
            festival_name,
            first_date,
            city
        FROM festival_dates
    ) sub
    ORDER BY norm_key
    ON CONFLICT (normalized_key) DO NOTHING
    RETURNING id, normalized_key
)
UPDATE festivals f
SET event_id = ins.id
FROM festival_dates fd
JOIN inserted ins
  ON ins.normalized_key = LOWER(fd.festival_name) || '|' || fd.year_str
WHERE f.id = fd.festival_id AND f.event_id IS NULL;

-- Link remaining festivals where the event already existed (DISTINCT ON skipped them)
WITH festival_dates AS (
    SELECT
        f.id AS festival_id,
        LOWER(f.festival_name) || '|' || EXTRACT(YEAR FROM MIN(s.date))::TEXT AS norm_key
    FROM festivals f
    JOIN shows s ON s.festival_id = f.id
    WHERE f.event_id IS NULL AND f.festival_name IS NOT NULL
    GROUP BY f.id, f.festival_name
)
UPDATE festivals f
SET event_id = e.id
FROM festival_dates fd
JOIN events e ON e.normalized_key = fd.norm_key
WHERE f.id = fd.festival_id AND f.event_id IS NULL;
