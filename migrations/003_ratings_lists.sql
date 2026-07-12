-- ── Ratings ──────────────────────────────────────────────────────────────────
ALTER TABLE shows ADD COLUMN IF NOT EXISTS rating DECIMAL(2,1)
    CHECK (rating IS NULL OR (rating >= 0.5 AND rating <= 5.0));

CREATE INDEX IF NOT EXISTS idx_shows_rating ON shows(user_id, rating DESC NULLS LAST)
    WHERE rating IS NOT NULL;

-- ── Lists ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lists (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL CHECK (char_length(title) <= 200),
    description TEXT CHECK (char_length(description) <= 1000),
    is_ranked   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
    updated_at  BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);

CREATE TABLE IF NOT EXISTS list_items (
    id       SERIAL PRIMARY KEY,
    list_id  INT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    show_id  INT NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    position INT NOT NULL DEFAULT 0,
    added_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
    UNIQUE(list_id, show_id)
);

CREATE INDEX IF NOT EXISTS idx_lists_user       ON lists(user_id);
CREATE INDEX IF NOT EXISTS idx_list_items_list  ON list_items(list_id, position);
CREATE INDEX IF NOT EXISTS idx_list_items_show  ON list_items(show_id);
