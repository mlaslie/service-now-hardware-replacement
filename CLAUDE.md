# Project notes for Claude

Real environment values (project, instance, people) are in the uncommitted `CLAUDE.local.md` and
`docs/HANDOFF.local.md`; committed files use placeholders (PROJECT_ID, INSTANCE, Jane/John Doe).

Start with `docs/HANDOFF.md` (then `docs/BACKLOG.md` for what is next): it covers resources, secrets (names only), GitHub and deploy
steps, problems already solved, lessons learned and open items.

- GCP project is **`PROJECT_ID`** (not the gcloud default). gcloud lives at
  `~/google-cloud-sdk/bin/gcloud`.
- Commits: repo-local identity is set; **no Claude/Anthropic co-author trailers or footers**.
- Push: `git push` to `origin` (private repo `OWNER/service-now-hardware-replacement`), only when asked.
- Unit tests: `uv run --group seed pytest` (check the exit code before committing; don't pipe it through `tail` in a commit chain). Model-routing evals: `uv run python evals/run.py`.
- ServiceNow admin tools (sign in with `seed/sn_seed.py login`): `scripts/sn_doctor.py` (permission checker), `scripts/sn_profile.py` (profile vs. instance), `scripts/sn_custom_role.py`.
- Run scripts from the repo root.
- Never commit personal data (`tests/test_no_pii.py` checks every tracked file against the local
  `.pii-patterns`): this environment's users live in the uncommitted `seed/users.json` (demo run sheet:
  `uv run python demo/build_demo.py` -> `demo/DEMO.local.html`); committed examples use Jane/John Doe at example.com.
  After changing `demo/DEMO.template.html`, rebuild the committed sheet with `--example`.
- Deploy: `./scripts/deploy.sh` (reads `.env`, which is local and not committed).
- Organization behaviour (names, colour, problem choices, rules) is `config/organization.yaml`, not code;
  check with `uv run python -m app.profile`. Docs: `docs/INSTALL.md`, `CONFIGURATION.md`, `ROLES.md`,
  `ARCHITECTURE.md`, `docs/site/index.html`.
- The agent acts in ServiceNow as the signed-in user. Never claim a change succeeded without
  reading it back (`_apply_changes` in `app/tools/tickets.py`).
