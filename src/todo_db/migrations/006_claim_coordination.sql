ALTER TABLE items ADD COLUMN claimed_session TEXT;
ALTER TABLE items ADD COLUMN claim_token TEXT;
ALTER TABLE items ADD COLUMN claimed_branch TEXT;
ALTER TABLE items ADD COLUMN claimed_worktree TEXT;
ALTER TABLE items ADD COLUMN git_baseline TEXT;
