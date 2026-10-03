# Monitoring

`./ops/observability.sh` prints what it would create; `./ops/observability.sh --apply` creates (or
updates) four log-based metrics and a Cloud Monitoring dashboard in `GOOGLE_CLOUD_PROJECT`:

| Metric | Counts | From the log line |
|---|---|---|
| `hw_tickets_filed` (labels `source`, `device_kind`) | Tickets filed, by intake path (tag, tag_fuzzy, description, photo_label, photo_match) and device kind | `ticket_filed {...}` (app/tools/filing.py) |
| `hw_self_help_fixed` | Requests the quick checks resolved, nothing filed | `self_help_fixed` |
| `hw_servicenow_failures` | ServiceNow calls that failed or timed out inside a tool | `ServiceNow call failed`, `ServiceNow unavailable` |
| `hw_signin_failures` | Turns where the user's ServiceNow sign-in was rejected or couldn't be checked | `ServiceNow rejected the forwarded user token`, `identity lookup failed` |

The dashboard (`ops/dashboard.json`) charts them hourly. Metrics count from the moment they exist;
earlier logs aren't backfilled. Alerting is left to you: in Cloud Monitoring, create an alert on
`hw_servicenow_failures` or `hw_signin_failures` above your normal rate.
