CREATE TABLE IF NOT EXISTS show_photos (
    id SERIAL PRIMARY KEY,
    show_id INT NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    event_id INT REFERENCES events(id) ON DELETE SET NULL,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    photo_url TEXT NOT NULL,
    caption TEXT,
    uploaded_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_show_photos_show_id ON show_photos (show_id);
CREATE INDEX IF NOT EXISTS idx_show_photos_event_id ON show_photos (event_id) WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_show_photos_user_id ON show_photos (user_id);
