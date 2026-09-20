PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS threads (
    thread_id   TEXT PRIMARY KEY,
    subject     TEXT,
    snippet     TEXT,
    message_count INTEGER DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL REFERENCES threads(thread_id),
    from_addr   TEXT,
    to_addr     TEXT,
    cc_addr     TEXT,
    subject     TEXT,
    date_str    TEXT,
    date_epoch  INTEGER,
    labels      TEXT,          -- JSON array of label IDs
    body_plain  TEXT DEFAULT '',
    body_html   TEXT DEFAULT '',
    size_bytes  INTEGER DEFAULT 0,
    snippet     TEXT,
    synced_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_thread     ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_date      ON messages(date_epoch);
CREATE INDEX IF NOT EXISTS idx_messages_from_addr ON messages(from_addr);
CREATE INDEX IF NOT EXISTS idx_messages_to_addr   ON messages(to_addr);
CREATE INDEX IF NOT EXISTS idx_messages_cc_addr   ON messages(cc_addr);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,  -- message_id + ':' + part_id
    message_id    TEXT NOT NULL REFERENCES messages(message_id),
    filename      TEXT,
    mime_type     TEXT,
    size_bytes    INTEGER DEFAULT 0,
    part_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO sync_state(key, value) VALUES ('messages_synced', '0');
INSERT OR IGNORE INTO sync_state(key, value) VALUES ('last_page_token', NULL);
INSERT OR IGNORE INTO sync_state(key, value) VALUES ('last_history_id', NULL);
INSERT OR IGNORE INTO sync_state(key, value) VALUES ('sync_started_at', NULL);

CREATE TABLE IF NOT EXISTS labels (
    label_id    TEXT PRIMARY KEY,
    label_name  TEXT UNIQUE,
    synced_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS people (
    email          TEXT PRIMARY KEY,
    display_name   TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    message_count  INTEGER DEFAULT 0,
    thread_count   INTEGER DEFAULT 0,
    initiated_count INTEGER DEFAULT 0,
    domain         TEXT,
    account        TEXT,
    is_internal         INTEGER DEFAULT 0,
    meeting_count       INTEGER DEFAULT 0,
    slack_user_id       TEXT,
    slack_message_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_people_display_name ON people(display_name);

CREATE TABLE IF NOT EXISTS slack_activity (
    channel_id    TEXT NOT NULL,
    channel_name  TEXT,
    account       TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    last_activity TEXT,
    synced_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel_id, account)
);

CREATE TABLE IF NOT EXISTS calendar_events (
    event_id        TEXT PRIMARY KEY,
    summary         TEXT,
    start_time      TEXT,
    end_time        TEXT,
    organizer_email TEXT,
    attendees       TEXT,   -- JSON array of {email, displayName}
    location        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_organizer ON calendar_events(organizer_email);
CREATE INDEX IF NOT EXISTS idx_calendar_events_start ON calendar_events(start_time);

CREATE TABLE IF NOT EXISTS thread_accounts (
    thread_id   TEXT NOT NULL REFERENCES threads(thread_id),
    account     TEXT NOT NULL,
    PRIMARY KEY (thread_id, account)
);

CREATE INDEX IF NOT EXISTS idx_thread_accounts_account ON thread_accounts(account);
