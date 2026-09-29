# Events and delivery

Each `check` fetches metadata, compares it to the SQLite baseline, queues configured
notifications and attempts due deliveries. Results and new/changed events are printed
to stdout as JSON. A failed check retains the previous successful snapshot; other
watches still run. Snapshot updates and pending events are committed atomically.

`check --dry-run` uses an in-memory copy of the existing baseline. It prints what
would change, without saving SQL state, sending notifications.

First-run previews include matching package records alongside the baseline result.

## Event types

Package identity includes suite, component, kind, name, architecture and full version.
All selected versions are retained in the snapshot.

- `package.added`: a new slot, or an older version added beside the current version.
- `package.updated`: a newly published version above the previous highest version.
- `package.downgraded`: the highest available version moves backwards.
- `package.repacked`: the same version has changed artifact names/checksums/sizes or
  source association. Mirror URL changes alone do not count.
- `package.removed`: all selected versions of a package slot disappear. Replacing
  versions and collecting older ones do not count as removal.
- `repository.error`: repeated check failures, or an immediate metadata/file-integrity failure.
- `repository.recovered`: a successful check after an announced failure.
- `readiness.timeout`: a source release remains incomplete beyond its configured timeout.

Related changes are grouped by type/source/version. New architecture changes can
merge into an existing waiting event. Changes arriving after delivery can create
another event; use `kinds: [source]` with binary readiness rules for a source-driven
build pipeline.

## Payloads

Webhooks receive `schema_version`, delivery `id`, `created_at`, and an `events` array.
Each event has its own stable `id`, `watch`, `repository`, `backend`, `observed_at`,
`type`, and relevant change details. Package events include source name/version and
full `old`/`new` package records. Captured sources carry original URLs, hashes, names,
sizes and local object paths. Times are UTC Unix seconds. Observation time means the
time of the first observation of this package by `gills`, locally.

HTTP headers:

- `X-Gills-Delivery`: stable delivery ID.
- Optional `X-Gills-Signature: sha256=...`: HMAC over the exact JSON bytes.

Delivery is at least once. Receivers should deduplicate build requests by event ID.
Timeouts after acceptance or crashes before recording success can cause duplicates.
Retry batches preserve their ID and exact body.

## Retries and digests

Destinations are independent. Failures retry after 30, 60, 120, ... seconds, capped
at an hour, indefinitely. Only `check` processes the queue, so the actual next attempt
is the next scheduled invocation after that deadline. Failed checks do not prevent
previously queued notifications being attempted.

Immediate destinations use one event per batch. Digests group up to 100 eligible
events once the oldest event has waited `digest_seconds`; `delivery_delay_seconds`
can additionally delay package events. A check does not stay running to wait for a
digest deadline or retry.

HTTP 2xx means accepted.

SMTP recipient refusal retries the batch. This means it may duplicate mail to
recipients that already accepted it.

Slack uses plain-text blocks and limits the displayed summary to 2,900 characters;
full data remains in SQLite and webhook/email JSON.

Signal assumes an existing linked signal-cli-rest-api service that is reachable to
`gills` somehow.

Updating a destination's configuration changes future attempts, including pending
ones. Removing a destination leaves its pending deliveries stored; restore the same
name to resume. Disabling it explicitly pauses delivery without reporting failure.
Already delivered notifications are not sent again by later checks.
