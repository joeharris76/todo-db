CREATE TABLE items (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL CHECK (length(title) BETWEEN 5 AND 200),
  worktree TEXT NOT NULL,
  priority TEXT NOT NULL CHECK (priority IN ('critical','high','medium-high','medium','low')),
  state TEXT NOT NULL DEFAULT 'planning' CHECK (state IN ('planning','active','done','dropped')),
  blocked_reason TEXT,
  category TEXT,
  description TEXT NOT NULL CHECK (length(description) >= 10),
  approach TEXT,
  claimed_by TEXT,
  claimed_at TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  completed_pr INTEGER
);

CREATE TABLE work_units (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  wid TEXT NOT NULL CHECK (wid GLOB 'w[0-9]' OR wid GLOB 'w[0-9][0-9]' OR wid GLOB 'w[0-9][0-9][0-9]'),
  summary TEXT NOT NULL CHECK (length(summary) BETWEEN 5 AND 200),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','in_progress','done')),
  evidence TEXT,
  notes TEXT,
  started_at TEXT,
  started_worktree TEXT,
  started_branch TEXT,
  PRIMARY KEY (item_id, wid)
);

CREATE TABLE work_needs (
  item_id TEXT NOT NULL,
  wid TEXT NOT NULL,
  needs_wid TEXT NOT NULL,
  PRIMARY KEY (item_id, wid, needs_wid),
  FOREIGN KEY (item_id, wid) REFERENCES work_units(item_id, wid) ON DELETE CASCADE,
  FOREIGN KEY (item_id, needs_wid) REFERENCES work_units(item_id, wid) ON DELETE CASCADE
);

CREATE TABLE item_deps (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  needs_item TEXT NOT NULL REFERENCES items(id),
  PRIMARY KEY (item_id, needs_item)
);

CREATE TABLE scope_rules (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('only_modify','do_not_modify')),
  path_glob TEXT NOT NULL,
  PRIMARY KEY (item_id, kind, path_glob)
);

CREATE TABLE verifications (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  description TEXT NOT NULL,
  command TEXT,
  expected TEXT,
  last_run TEXT,
  last_result TEXT CHECK (last_result IN ('pass','fail') OR last_result IS NULL),
  PRIMARY KEY (item_id, seq)
);

CREATE TABLE preserves (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  behavior TEXT NOT NULL,
  PRIMARY KEY (item_id, behavior)
);

CREATE TABLE anti_patterns (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  dont TEXT NOT NULL,
  why TEXT NOT NULL,
  instead TEXT NOT NULL,
  PRIMARY KEY (item_id, dont)
);

CREATE TABLE prior_art (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  concept TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('reuse','extend','supersede')),
  PRIMARY KEY (item_id, path, concept)
);

CREATE TABLE deferrals (
  id INTEGER PRIMARY KEY,
  from_item TEXT NOT NULL REFERENCES items(id),
  summary TEXT NOT NULL,
  reason TEXT NOT NULL,
  resolution TEXT NOT NULL DEFAULT 'open' CHECK (resolution IN ('open','promoted','dismissed')),
  resolved_item TEXT REFERENCES items(id),
  resolved_reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX idx_items_state ON items(state);
CREATE INDEX idx_deferrals_open ON deferrals(from_item, resolution);
