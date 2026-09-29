# Configuration reference

YAML keys are validated automatically. Unknown keys are rejected; booleans must be
actual booleans. Paths are resolved relative to the YAML file.

## Root

| Key | Default | Meaning |
| --- | --- | --- |
| `version` | required `1` | YAML format |
| `state_dir` | `./state` | SQLite baseline and notification history |
| `notify` | `true` | Queue/deliver configured notifications on each check |
| `timeout_seconds` | `30` | HTTP/SMTP socket timeout |
| `max_index_bytes` | `536870912` | Per compressed/decompressed index limit |
| `watches` | required | Nonempty list of repositories/selections |
| `destinations` | `{}` | Named notification destinations; empty means none |

Every invocation runs once. Scheduling belongs to cron or a systemd timer.
Timeouts bound socket inactivity.

## Watch

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | required | Stable unique letters/digits/hyphen/underscore name |
| `type` | required | `apt` or `rpm` |
| `url` | required | Repository HTTPS base URL, no credentials/query |
| `allow_http` | `false` | Explicitly permit plain HTTP for this watch |
| `allow_private_networks` | `false` | Permit non-public addresses for this watch |
| `suites` | APT required; RPM `[default]` | Explicit APT suites; one RPM label |
| `components` | `[main]` | APT components; RPM fixed to `[main]` |
| `architectures` | APT `[amd64]`; RPM `[x86_64]` | Binary targets |
| `kinds` | `[source]` | `source`, `binary`, or both |
| `headers_env` | absent | Name of environment variable with JSON HTTP headers |
| `filters` | `{}` | Package/source/version rules |
| `destinations` | `[]` | Names of notification destinations selected by this watch |
| `events` | added, updated, downgraded, repacked | Package event types |
| `version_policy` | `any` | `any`, `upstream`, `packaging` |
| `notify_initial` | `false` | Announce existing matching packages at baseline |
| `group_by_source` | `true` | Group by event type, source name and exact source version |
| `readiness` | `{}` | Publication requirements below |
| `delivery_delay_seconds` | `0` | Delay ready package notifications |
| `failure_threshold` | `3` | Consecutive failures before alert; integrity errors alert immediately |
| `max_release_age_seconds` | `0` | APT Release Date age limit; zero disables |

Event names: `package.added`, `package.updated`, `package.downgraded`,
`package.repacked`, `package.removed`. Health events (`repository.error`,
`repository.recovered`, `readiness.timeout`) are generated as needed and can be
routed using destination filters. No GPG or signing-key settings are required.

APT reads `Release`, falling back to the cleartext metadata in `InRelease` if
`Release` is absent. RPM reads `repodata/repomd.xml` and its primary index. Signatures
are not fetched/verified. SHA256 and sizes are checked; APT dates and `Valid-Until`
are checked for freshness. HTTPS provides transport authentication.

## Filters

Rules combine with AND; globs within one list combine with OR. Globs are case-sensitive;
regexes use Python `re.search`, supporting flags such as `(?i)`.

| Key | Meaning |
| --- | --- |
| `packages`, `sources` | Included package/source name globs |
| `exclude_packages`, `exclude_sources` | Excluded package/source name globs |
| `package_regex`, `source_regex` | Additional name expressions |
| `version_include`, `version_exclude` | Version expressions |
| `version_field` | `full` (default) or `upstream` for version expressions |
| `min_version` | Minimum full native version; quote it as a string |

Native Debian/RPM comparators handle epochs, revisions and prereleases.
`8.4.1-1` → `8.4.2-1` is an upstream change; `8.4.1-1` → `8.4.1-2` is packaging.
Epoch changes count as upstream changes. Vendor suffixes within an upstream-version
component remain part of that component. Policies filter transitions; additions,
removals and repacks are controlled by their event types. Filtered transitions
still advance the baseline.

Changing URL/backend/suites/components/architectures/kinds/filters creates a new
selection scope and quietly rebaselines unless `notify_initial` is enabled.
Previously waiting events are superseded; already queued deliveries remain.

## Readiness

```yaml
readiness:
  require_source: true
  binaries: ['php*-cli', 'php*-common']
  architectures: [amd64, arm64]
  suites: [trixie]
  timeout_seconds: 3600
```

Defaults: require source, no binary patterns, watch architectures/suites, timeout
3600 seconds. Each pattern must match at least one binary from the same source
name and exact source version for every required architecture/suite. `all`/`noarch`
satisfy any architecture. For specific binaries use exact names.

Requirements check index publication.

Multi-suite gates require identical full source versions; use separate watches where
distro suffixes differ. Timeout alerts once; the event remains waiting and can become
ready on a later check.

## Metadata and RPM

Only repository indexes are downloaded and processed in memory. Package versions,
URLs and checksums are recorded as metadata in SQLite.

One RPM watch covers one concrete base URL. Watch a separate SRPM repository directly
for source-driven builds. Binary-to-source RPM mapping assumes the epochs agree
because the `sourcerpm` filename omits the source epoch. The APT 'freshness-age'
setting does not apply to RPM metadata.

## Destinations

Define named destinations in the top-level `destinations` mapping. Each watch
selects its targets with `destinations: [builds, email]`; empty or omitted means
no notifications for that watch. Unknown destination names are rejected.

All destinations accept `enabled` (default true), `events`, and
`digest_seconds` (default zero). Empty/omitted `events` accepts all event types.

Routing changes affect newly queued events, including health/readiness events.
Already queued notifications retain their destinations and retry normally; use
`enabled: false` to pause a destination and its existing retries. Changing a watch's
destinations does not reset its repository baseline.

- `webhook`: exactly one of `url` or `url_env`; optional `headers_env`, `secret_env`.
- `slack`: incoming webhook `url` or `url_env`; optional headers.
- `signal`: full `/v2/send` `url` or `url_env`, `number_env`, `recipients` list.
- `email`: `host`, `from`, `to`; `tls: starttls|ssl|none`; optional `port`,
  `username_env`, `password_env`. Ports default to 587 or 465 for SSL.
- `stdout`: emit delivery envelopes on stdout in addition to the check result.

`headers_env` names an environment variable holding a JSON object of string headers.
`secret_env` adds HMAC-SHA256 to HTTP payloads. URLs/credentials are resolved at send
time. Configure notification credentials in the service or cron environment. HTTP POST
redirects are refused; GET credentials are stripped on cross-origin redirects and
HTTPS cannot redirect to HTTP.

`notify: false` disables new queueing and pauses existing deliveries globally.
`enabled: false` does the same for one destination. Re-enabling resumes older pending
retries; changes observed while disabled are not queued retrospectively.

## Network policy

HTTPS is the default for repositories and HTTP notification destinations, including
URLs supplied through environment variables. Certificate chains and hostnames are
always validated using the system/Python trust store; there is no verify-false mode.
Use the system trust store (or SSL_CERT_FILE/SSL_CERT_DIR) for an internal CA.

`allow_private_networks: true` permits internal addresses **only** for that watch or
HTTP destination. `allow_http: true` separately permits plain HTTP. Both default to
`false`. Policies apply to every request and redirect.

HTTP proxies from the environment are ignored to prevent proxy-side resolution
bypassing address checks.

SMTP is an explicitly configured service and may be local; its TLS settings remain
`starttls`, `ssl` or an explicit `none`. SMTP TLS also validates certificates.

## SQLite retention and the 'prune' command

SQLite uses WAL and synchronous FULL. A process lock serialises mutating operations
on a local state directory. Repository indexes are processed in memory.

`prune --days 30` removes completed events older than 30 days, associated delivery
records and unreferenced completed batches, then compacts SQLite.

A recent delivery keeps its event until the retention period has passed.
