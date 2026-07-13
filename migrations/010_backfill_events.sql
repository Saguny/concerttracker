-- Deduplicate and insert events for all existing standalone shows
INSERT INTO events (normalized_key, artist, date, venue, city)
SELECT DISTINCT ON (LOWER(artist) || '|' || date::text || '|' || LOWER(COALESCE(venue, '')))
    LOWER(artist) || '|' || date::text || '|' || LOWER(COALESCE(venue, '')) AS normalized_key,
    artist,
    date,
    venue,
    city
FROM shows
WHERE (is_festival = FALSE OR festival_name IS NULL)
  AND artist IS NOT NULL
  AND date IS NOT NULL
  AND event_id IS NULL
ON CONFLICT (normalized_key) DO NOTHING;

-- Link each existing show to its event
UPDATE shows s
SET event_id = e.id
FROM events e
WHERE (s.is_festival = FALSE OR s.festival_name IS NULL)
  AND s.event_id IS NULL
  AND s.artist IS NOT NULL
  AND s.date IS NOT NULL
  AND e.normalized_key = LOWER(s.artist) || '|' || s.date::text || '|' || LOWER(COALESCE(s.venue, ''));
