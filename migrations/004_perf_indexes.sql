-- Covers ORDER BY created_at DESC for the social feed and profile recent-shows queries,
-- both of which filter by user_id first then sort by created_at.
CREATE INDEX IF NOT EXISTS idx_shows_user_created ON shows(user_id, created_at DESC);
