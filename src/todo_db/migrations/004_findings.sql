CREATE TABLE findings (
  id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 3 AND 200),
  date TEXT NOT NULL,
  finding_kind TEXT NOT NULL,
  review_context TEXT NOT NULL,
  observed_sha TEXT,
  title TEXT NOT NULL,
  finding_text TEXT NOT NULL,
  why_matters TEXT NOT NULL,
  next_steps TEXT NOT NULL,
  disposition TEXT NOT NULL DEFAULT 'open' CHECK (disposition IN ('open','actionable','actioned','dismissed','promoted')),
  disposition_reason TEXT,
  urgency TEXT,
  breadth TEXT,
  confidence TEXT,
  reconsider_after TEXT,
  created_at TEXT NOT NULL,
  imported_from TEXT,
  CHECK (disposition NOT IN ('actionable','dismissed') OR (disposition_reason IS NOT NULL AND length(disposition_reason) > 0))
);

CREATE TABLE finding_evidence (
  id INTEGER PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  pattern TEXT,
  line_start INTEGER,
  line_end INTEGER,
  note TEXT
);

CREATE TABLE finding_links (
  id INTEGER PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('promoted-to','informs','resolved-by','related-finding','duplicate-of')),
  target_item TEXT REFERENCES items(id),
  target_finding TEXT REFERENCES findings(id),
  note TEXT
);

CREATE TABLE finding_events (
  seq INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  finding_id TEXT REFERENCES findings(id),
  action TEXT NOT NULL,
  detail TEXT
);

CREATE INDEX idx_findings_disposition ON findings(disposition);
CREATE INDEX idx_finding_events_finding ON finding_events(finding_id, seq);
