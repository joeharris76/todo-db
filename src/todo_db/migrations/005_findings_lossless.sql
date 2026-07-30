-- Bring findings storage level with BenchBox's v4 findings schema so a legacy
-- snapshot can be restored losslessly. Mirrors
-- _project/scripts/todo_findings.py::finding_migration_v4_statements() in the
-- BenchBox checkout; without these, restore_legacy has nowhere to put
-- related_paths, suggested_sweep or a finding's section bodies, and the
-- migration silently drops them.

ALTER TABLE findings ADD COLUMN related_paths TEXT;
ALTER TABLE findings ADD COLUMN suggested_sweep TEXT;

CREATE TABLE finding_sections (
  id INTEGER PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  heading TEXT NOT NULL,
  text TEXT NOT NULL,
  UNIQUE (finding_id, position)
);

CREATE INDEX idx_finding_sections_finding ON finding_sections(finding_id, position);

-- Rebuild finding_links so target_item's reference to items(id) is DEFERRABLE.
-- A restore loads tables one at a time, so a finding that points at an item can
-- be inserted before that item exists; without deferral the insert order alone
-- decides whether the restore succeeds.
CREATE TABLE finding_links_v5 (
  id INTEGER PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('promoted-to','informs','resolved-by','related-finding','duplicate-of')),
  target_item TEXT REFERENCES items(id) DEFERRABLE INITIALLY DEFERRED,
  target_finding TEXT REFERENCES findings(id),
  note TEXT
);

INSERT INTO finding_links_v5 (id, finding_id, kind, target_item, target_finding, note)
SELECT id, finding_id, kind, target_item, target_finding, note FROM finding_links;

DROP TABLE finding_links;

ALTER TABLE finding_links_v5 RENAME TO finding_links;
