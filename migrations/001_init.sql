CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
    invite_code_used TEXT
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code TEXT PRIMARY KEY,
    created_by INT REFERENCES users(id) ON DELETE SET NULL,
    used_by INT REFERENCES users(id) ON DELETE SET NULL,
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
    used_at BIGINT
);

CREATE TABLE IF NOT EXISTS shows (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    artist TEXT NOT NULL,
    venue TEXT NOT NULL,
    city TEXT NOT NULL,
    date DATE NOT NULL,
    is_festival BOOLEAN NOT NULL DEFAULT FALSE,
    festival_name TEXT,
    notes TEXT,
    setlist JSONB,
    artist_mbid TEXT,
    artist_spotify_id TEXT,
    artist_image_url TEXT,
    artist_thumb_url TEXT,
    artist_genres TEXT[],
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);

CREATE TABLE IF NOT EXISTS show_attendees (
    show_id INT NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (show_id, user_id)
);

CREATE TABLE IF NOT EXISTS follows (
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
    PRIMARY KEY (user_id, target_id),
    CHECK (user_id <> target_id)
);

CREATE INDEX IF NOT EXISTS idx_shows_user_date ON shows(user_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_shows_date ON shows(date);
CREATE INDEX IF NOT EXISTS idx_shows_artist ON shows(user_id, artist);
CREATE INDEX IF NOT EXISTS idx_show_attendees_show ON show_attendees(show_id);
CREATE INDEX IF NOT EXISTS idx_show_attendees_user ON show_attendees(user_id);
CREATE INDEX IF NOT EXISTS idx_follows_user ON follows(user_id);
CREATE INDEX IF NOT EXISTS idx_follows_target ON follows(target_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- show lineup
ALTER TABLE shows ADD COLUMN IF NOT EXISTS support_acts TEXT[];

-- festival-level notes (stored on one representative row, keyed by festival_name)
ALTER TABLE shows ADD COLUMN IF NOT EXISTS festival_notes TEXT;

-- profile extensions
ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;

-- show interactions
CREATE TABLE IF NOT EXISTS show_comments (
    id SERIAL PRIMARY KEY,
    show_id INT NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL CHECK (char_length(body) <= 500),
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);

CREATE TABLE IF NOT EXISTS show_likes (
    show_id INT NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (show_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_show_comments_show ON show_comments(show_id);
CREATE INDEX IF NOT EXISTS idx_show_comments_user ON show_comments(user_id);
CREATE INDEX IF NOT EXISTS idx_show_likes_show ON show_likes(show_id);

CREATE TABLE IF NOT EXISTS artist_comments (
    id SERIAL PRIMARY KEY,
    artist_name TEXT NOT NULL,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL CHECK (char_length(body) <= 500),
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_artist_comments_name ON artist_comments(artist_name);

-- festivals table: one row per festival submission per user
CREATE TABLE IF NOT EXISTS festivals (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    festival_name TEXT NOT NULL,
    city TEXT,
    festival_notes TEXT,
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
    UNIQUE(user_id, festival_name)
);

-- remove the broken UUID column if it exists, then add INT FK
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'shows' AND column_name = 'festival_id' AND udt_name = 'uuid'
    ) THEN
        ALTER TABLE shows DROP COLUMN festival_id;
    END IF;
END $$;

ALTER TABLE shows ADD COLUMN IF NOT EXISTS festival_id INT REFERENCES festivals(id);

-- backfill festivals from shows (idempotent: ON CONFLICT DO NOTHING)
INSERT INTO festivals (user_id, festival_name, city, festival_notes, created_at)
SELECT
    s.user_id,
    s.festival_name,
    MIN(s.city),
    MIN(s.festival_notes),
    MIN(s.created_at)
FROM shows s
WHERE s.is_festival = TRUE AND s.festival_name IS NOT NULL
GROUP BY s.user_id, s.festival_name
ON CONFLICT DO NOTHING;

-- link shows to their festival row (idempotent: only updates NULL rows)
UPDATE shows s
SET festival_id = f.id
FROM festivals f
WHERE s.user_id = f.user_id AND s.festival_name = f.festival_name
  AND s.festival_id IS NULL AND s.is_festival = TRUE;

CREATE INDEX IF NOT EXISTS idx_shows_festival_id ON shows(festival_id);
CREATE INDEX IF NOT EXISTS idx_festivals_user ON festivals(user_id);

-- username change history tracking
ALTER TABLE users ADD COLUMN IF NOT EXISTS prev_username TEXT;

-- profile customization
ALTER TABLE users ADD COLUMN IF NOT EXISTS banner_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS accent_color TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS location TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS favorite_artists TEXT[];
ALTER TABLE users ADD COLUMN IF NOT EXISTS social_links JSONB;
ALTER TABLE users ADD COLUMN IF NOT EXISTS pinned_show_id INT REFERENCES shows(id) ON DELETE SET NULL;

-- global artist catalogue
CREATE TABLE IF NOT EXISTS artists (
    name TEXT PRIMARY KEY,
    spotify_id TEXT,
    image_url TEXT,
    thumb_url TEXT,
    genres TEXT[],
    created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
    updated_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);
