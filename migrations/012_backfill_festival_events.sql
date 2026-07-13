-- Create events for existing festivals (keyed by festival_name + year + city)
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
            LOWER(festival_name) || '|' || year_str || '|' || LOWER(COALESCE(city, '')) AS norm_key,
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
  ON ins.normalized_key = LOWER(fd.festival_name) || '|' || fd.year_str || '|' || LOWER(COALESCE(fd.city, ''))
WHERE f.id = fd.festival_id AND f.event_id IS NULL;

-- Also link any remaining festivals that now have an existing event but no event_id
-- (handles duplicate festivals that weren't first in the DISTINCT ON)
WITH festival_dates AS (
    SELECT
        f.id AS festival_id,
        LOWER(f.festival_name) || '|' || EXTRACT(YEAR FROM MIN(s.date))::TEXT || '|' || LOWER(COALESCE(f.city, '')) AS norm_key
    FROM festivals f
    JOIN shows s ON s.festival_id = f.id
    WHERE f.event_id IS NULL AND f.festival_name IS NOT NULL
    GROUP BY f.id, f.festival_name, f.city
)
UPDATE festivals f
SET event_id = e.id
FROM festival_dates fd
JOIN events e ON e.normalized_key = fd.norm_key
WHERE f.id = fd.festival_id AND f.event_id IS NULL;
