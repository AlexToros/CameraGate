ALTER TABLE users ADD COLUMN failed_logins integer NOT NULL DEFAULT 0 CHECK (failed_logins BETWEEN 0 AND 3);
CREATE TABLE password_resets (
 user_id bigint PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 token_hash text NOT NULL UNIQUE,
 creator_id bigint NOT NULL REFERENCES users(id),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '1 day'
);
