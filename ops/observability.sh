#!/usr/bin/env bash
# Creates (or updates) log-based metrics and a Cloud Monitoring dashboard for the agent (G4.2).
#
#   ./ops/observability.sh            # prints what it would do
#   ./ops/observability.sh --apply    # creates/updates them in GOOGLE_CLOUD_PROJECT
#
# Reads .env (GOOGLE_CLOUD_PROJECT, SERVICE_NAME). Needs roles/logging.configWriter and
# roles/monitoring.dashboardEditor. Safe to rerun.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then set -a; source .env; set +a; fi
: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in .env}"
SERVICE="${SERVICE_NAME:-hardware-replacement-agent}"
APPLY="${1:-}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

uv run python - "$SERVICE" "$TMP" <<'PY'
import json, sys
from pathlib import Path
import yaml
service, out = sys.argv[1], Path(sys.argv[2])
for m in yaml.safe_load(Path("ops/metrics.yaml").read_text()):
    cfg = {"name": m["name"], "description": m["description"], "filter": m["filter"].replace("SERVICE", service),
           "metricDescriptor": {"metricKind": "DELTA", "valueType": "INT64",
                                "labels": [{"key": k, "valueType": "STRING"} for k in m.get("labels", {})]}}
    if m.get("labels"):
        cfg["labelExtractors"] = m["labels"]
    (out / f"{m['name']}.json").write_text(json.dumps(cfg, indent=1))
PY

for f in "$TMP"/*.json; do
  name="$(basename "$f" .json)"
  if [[ "$APPLY" != "--apply" ]]; then echo "would create/update metric $name"; continue; fi
  if gcloud logging metrics describe "$name" --project="$GOOGLE_CLOUD_PROJECT" >/dev/null 2>&1; then
    gcloud logging metrics update "$name" --config-from-file="$f" --project="$GOOGLE_CLOUD_PROJECT" --quiet
  else
    gcloud logging metrics create "$name" --config-from-file="$f" --project="$GOOGLE_CLOUD_PROJECT" --quiet
  fi
done

if [[ "$APPLY" != "--apply" ]]; then
  echo "would create dashboard from ops/dashboard.json"; echo "Rerun with --apply to create them."; exit 0
fi
gcloud monitoring dashboards create --config-from-file=ops/dashboard.json --project="$GOOGLE_CLOUD_PROJECT"
echo "Dashboard: https://console.cloud.google.com/monitoring/dashboards?project=${GOOGLE_CLOUD_PROJECT}"
