"""Database schema (design doc section 12)."""

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id                  TEXT PRIMARY KEY,
    parent_id           TEXT REFERENCES nodes(id),
    node_type           TEXT NOT NULL,
    title               TEXT NOT NULL,
    current_revision_id TEXT,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    state               TEXT NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active','archived','merged')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);

CREATE TABLE IF NOT EXISTS node_revisions (
    id                  TEXT PRIMARY KEY,
    node_id             TEXT NOT NULL REFERENCES nodes(id),
    revision_no         INTEGER NOT NULL,
    parent_id           TEXT,
    node_type           TEXT NOT NULL,
    title               TEXT NOT NULL,
    body                TEXT NOT NULL,
    change_type         TEXT NOT NULL,
    source_candidate_id TEXT,
    content_hash        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (node_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_revisions_node ON node_revisions(node_id);

CREATE TABLE IF NOT EXISTS relations (
    id                     TEXT PRIMARY KEY,
    from_node_id           TEXT NOT NULL REFERENCES nodes(id),
    to_node_id             TEXT NOT NULL REFERENCES nodes(id),
    relation_type          TEXT NOT NULL,
    is_directed            INTEGER NOT NULL DEFAULT 1,
    label                  TEXT,
    rationale              TEXT,
    from_revision_id       TEXT NOT NULL REFERENCES node_revisions(id),
    to_revision_id         TEXT NOT NULL REFERENCES node_revisions(id),
    confidence             REAL,
    origin                 TEXT NOT NULL CHECK (origin IN ('ai','user')),
    model_profile          TEXT,
    state                  TEXT NOT NULL DEFAULT 'ai_generated'
                           CHECK (state IN ('ai_generated','confirmed','rejected','stale')),
    supersedes_relation_id TEXT,
    valid_from             TEXT NOT NULL,
    valid_to               TEXT
);
-- Business uniqueness for live relations (undirected pairs are normalised
-- before insert so from_node_id <= to_node_id when is_directed = 0).
CREATE UNIQUE INDEX IF NOT EXISTS idx_relations_unique_active
    ON relations(from_node_id, to_node_id, relation_type)
    WHERE state IN ('ai_generated','confirmed');
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_node_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_node_id);

CREATE TABLE IF NOT EXISTS candidates (
    id                        TEXT PRIMARY KEY,
    source_binding_id         TEXT,
    candidate_type            TEXT NOT NULL,
    source_excerpt_ciphertext BLOB,
    normalized_content        TEXT NOT NULL,
    title                     TEXT NOT NULL,
    proposed_action           TEXT NOT NULL,
    proposed_parent_ids       TEXT NOT NULL DEFAULT '[]',
    confidence                REAL,
    needs_clarification       INTEGER NOT NULL DEFAULT 0,
    status                    TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','confirmed','rejected','snoozed')),
    batch_date                TEXT,
    message_hmac              TEXT,
    created_at                TEXT NOT NULL,
    decided_at                TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);

CREATE TABLE IF NOT EXISTS source_bindings (
    id              TEXT PRIMARY KEY,
    binding_key     TEXT NOT NULL,
    channel         TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    sender_key      TEXT NOT NULL,
    conversation_id TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    passive_capture INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_cursors (
    source_binding_id    TEXT PRIMARY KEY REFERENCES source_bindings(id),
    last_successful_time TEXT,
    last_message_hmac    TEXT,
    last_job_status      TEXT,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_receipts (
    source_binding_id  TEXT NOT NULL,
    message_hmac       TEXT NOT NULL,
    capture_mode       TEXT NOT NULL CHECK (capture_mode IN ('explicit','daily')),
    handled_explicitly INTEGER NOT NULL DEFAULT 0,
    captured_at        TEXT NOT NULL,
    processed_at       TEXT,
    batch_id           TEXT,
    PRIMARY KEY (source_binding_id, message_hmac)
);

CREATE TABLE IF NOT EXISTS embeddings (
    node_id     TEXT NOT NULL REFERENCES nodes(id),
    revision_id TEXT NOT NULL REFERENCES node_revisions(id),
    model_id    TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    vector_blob BLOB NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (node_id, model_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id           TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    before_json  TEXT,
    after_json   TEXT,
    candidate_id TEXT,
    event_hmac   TEXT,
    confirmed_at TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS view_layout (
    view_id    TEXT NOT NULL,
    node_id    TEXT NOT NULL REFERENCES nodes(id),
    x          REAL,
    y          REAL,
    width      REAL,
    height     REAL,
    collapsed  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (view_id, node_id)
);

CREATE TABLE IF NOT EXISTS export_log (
    id         TEXT PRIMARY KEY,
    scope_json TEXT NOT NULL,
    format     TEXT NOT NULL,
    file_hash  TEXT NOT NULL,
    plaintext  INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id          TEXT PRIMARY KEY,
    job_type    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    messages_in INTEGER NOT NULL DEFAULT 0,
    candidates_out INTEGER NOT NULL DEFAULT 0,
    error_kind  TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    node_id UNINDEXED,
    title,
    body,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS personal_dictionary (
    term       TEXT PRIMARY KEY,
    note       TEXT,
    created_at TEXT NOT NULL
);
"""

# Applied on every open so existing vaults pick up new invariants (section 12.4).
EXTRA_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_nodes_current_revision_insert
BEFORE INSERT ON nodes
FOR EACH ROW
WHEN NEW.current_revision_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'current_revision_id must belong to this node')
  WHERE NOT EXISTS (
    SELECT 1 FROM node_revisions
    WHERE id = NEW.current_revision_id AND node_id = NEW.id
  );
END;

CREATE TRIGGER IF NOT EXISTS trg_nodes_current_revision_update
BEFORE UPDATE OF current_revision_id ON nodes
FOR EACH ROW
WHEN NEW.current_revision_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'current_revision_id must belong to this node')
  WHERE NOT EXISTS (
    SELECT 1 FROM node_revisions
    WHERE id = NEW.current_revision_id AND node_id = NEW.id
  );
END;

CREATE TRIGGER IF NOT EXISTS trg_nodes_type_insert
BEFORE INSERT ON nodes
FOR EACH ROW
WHEN NEW.node_type NOT IN
  ('idea','opinion','decision','question','principle','hypothesis','insight','risk','method','topic')
BEGIN
  SELECT RAISE(ABORT, 'invalid node_type');
END;

CREATE TRIGGER IF NOT EXISTS trg_nodes_type_update
BEFORE UPDATE OF node_type ON nodes
FOR EACH ROW
WHEN NEW.node_type NOT IN
  ('idea','opinion','decision','question','principle','hypothesis','insight','risk','method','topic')
BEGIN
  SELECT RAISE(ABORT, 'invalid node_type');
END;

CREATE TRIGGER IF NOT EXISTS trg_revisions_type_insert
BEFORE INSERT ON node_revisions
FOR EACH ROW
WHEN NEW.node_type NOT IN
  ('idea','opinion','decision','question','principle','hypothesis','insight','risk','method','topic')
BEGIN
  SELECT RAISE(ABORT, 'invalid revision node_type');
END;

CREATE TRIGGER IF NOT EXISTS trg_revisions_delete_current
BEFORE DELETE ON node_revisions
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM nodes WHERE current_revision_id = OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'cannot delete a current node revision');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_events
BEGIN
  SELECT RAISE(ABORT, 'audit_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_events
BEGIN
  SELECT RAISE(ABORT, 'audit_events are append-only');
END;
"""
