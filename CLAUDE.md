# Project notes for Claude

Start with `docs/HANDOFF.md` (then `docs/BACKLOG.md` for what is next): it covers resources, secrets (names only), GitHub and deploy
steps, problems already solved, lessons learned and open items.

- GCP project is **`PROJECT_ID`** (not the gcloud default). gcloud lives at
  `~/google-cloud-sdk/bin/gcloud`.
- Commits: repo-local identity is set; **no Claude/Anthropic co-author trailers or footers**.
- Push: `git push` to `origin` (private repo `OWNER/service-now-hardware-replacement`), only when asked.
- Tests: `uv run --group seed pytest`. Deploy: `./scripts/deploy.sh`.
- The agent acts in ServiceNow as the signed-in user. Never claim a change succeeded without
  reading it back (`_apply_changes` in `app/tools.py`).
