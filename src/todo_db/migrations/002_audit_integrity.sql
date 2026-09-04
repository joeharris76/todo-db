ALTER TABLE events ADD COLUMN hash_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE audit_head (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    head_seq INTEGER NOT NULL,
    head_hash TEXT
);

INSERT INTO audit_head(singleton, head_seq, head_hash) VALUES (1, 0, NULL);
