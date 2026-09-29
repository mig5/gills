<p align="center"><img src="https://git.mig5.net/mig5/gills/raw/branch/main/assets/gills.png" alt="A friendly salmon chasing deb and rpm parcels upstream" width="260"></p>

# Gills

Swimming upstream for new packages!

Watch APT and RPM repositories for package changes and notify your build pipeline.

Gills uses SQLite and has two commands:

```sh
gills -c config.yml check
gills -c config.yml prune
```

 * `check` runs once. Cron or a systemd timer can invoke it periodically. Notifications
and their retries happen automatically during checks, according to your YAML.
 * `prune` deletes old completed SQLite history while retaining baselines and pending work.

## Get started

Python 3.10+ is required. Install from this source tree with Poetry:

```sh
poetry install
cp examples/sury.yml config.yml
poetry run gills -c config.yml check --dry-run
poetry run gills -c config.yml check
```

Or use a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/gills -c config.yml check
```

The following Sury example watches PHP 8.3 and 8.4 sources in `trixie/main`, excludes
alpha/beta/RC versions and waits for their matching CLI binaries on `amd64` before
notifying.

A minimal configuration:

```yaml
version: 1
state_dir: ./state
notify: true
watches:
  - name: sury-php
    type: apt
    url: https://packages.sury.org/php/
    suites: [trixie]
    kinds: [source]
    filters:
      sources: ['php8.3', 'php8.4']
      version_exclude: '(?i)(alpha|beta|rc)'
destinations: {}
```

All paths are relative to the YAML file. An empty `destinations` mapping means
results only appear on stdout. Remove `filters` to watch all packages in the
configured suites/components/architectures. Set `kinds: [source, binary]` to watch
both types.

The first successful check quietly establishes a baseline. Set
`notify_initial: true` on a watch to announce existing matching packages too.
Subsequent checks report additions, version changes and changed checksums.

## Dry run

```sh
gills -c config.yml check --dry-run
```

Prints the check results and proposed events as JSON. An existing SQLite baseline
is read through a read-only connection and copied into memory. The real database and
notification queue are not updated. A first-ever dry run does not create
a state directory. Temporary metadata downloads are discarded afterwards.
Notifications are skipped. The command returns after one check.

## Notifications

Copy the desired destinations from `examples/destinations.yml`. Supported outputs
are email (SMTP), webhooks, Slack incoming webhooks, signal-cli-rest-api and stdout.
Select their names in each watch with `destinations: [builds, email]`.
An empty or omitted watch list means no notifications. Destination definitions
do not accept `watches`.

For example:

```yaml
version: 1
notify: true
watches:
  - name: sury-php
    type: apt
    url: https://packages.sury.org/php/
    suites: [trixie]
    destinations: [builds, email]

destinations:
  builds:
    type: webhook
    url_env: BUILD_WEBHOOK_URL
    secret_env: BUILD_WEBHOOK_SECRET
    events: [package.added, package.updated, package.repacked]
  email:
    type: email
    host: smtp.example.org
    tls: starttls
    username_env: SMTP_USERNAME
    password_env: SMTP_PASSWORD
    from: gills@example.org
    to: [builds@example.org]
```

Configure credentials through the named environment variables. `notify: false`
continues tracking repository changes and printing results, while disabling both
new notification queuing and delivery. Existing retries remain paused. Set
`enabled: false` on an individual destination to disable just that destination.
Changes observed while notifications are disabled are not sent retroactively.

Each ordinary `check` automatically attempts due deliveries, including retries
from previous checks.

Failed destinations retry independently with persistent backoff.

Cron/timer frequency determines when a due retry is attempted. Digests are
available through `digest_seconds`.

Webhooks receive JSON with stable event IDs, so receivers can deduplicate build
requests. Optional HMAC-SHA256 signs the exact JSON body. Delivery is at least once:
a timeout after the receiver accepted a request may result in a duplicate.

## Filtering and readiness

- Select suites, components, architectures and source/binary package kinds.
- Include/exclude package and source globs, regexes and native version ranges.
- `version_policy: upstream` triggers on upstream-version changes;
  `version_policy: packaging` selects packaging revisions; `any` selects both.
- Group related binary changes by source name and source version.
- Use `readiness` to wait for source and required binary metadata before notifying.

See [configuration](docs/configuration.md) and [event/delivery details](docs/events.md).

## Scheduling and SQLite cleanup

Cron, every five minutes:

```cron
*/5 * * * * /usr/bin/gills -c /etc/gills/config.yml check >>/var/log/gills.log 2>&1
```

The templates in `packaging/systemd/` invoke the same one-shot `check`. For a native
package installation, create an `gills` service account, put the YAML at
`/etc/gills/config.yml`, and set `state_dir: /var/lib/gills`. Copy the service
and timer into `/etc/systemd/system`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now gills.timer
```

For a virtualenv installation, adjust the service's `ExecStart` executable path.
A service environment file can be placed at `/etc/gills/gills.env`.

```sh
gills -c config.yml prune            # Delete completed SQLite history older than 30 days
gills -c config.yml prune --days 7
```

Prune removes old completed events, their delivery records and unreferenced completed
batches, then compacts SQLite. The current package baseline, waiting events,
pending retries and recently delivered history remain. This prevents duplicate
notifications after pruning.

Back up SQLite using its online-backup API, or stop checks before copying state.

HTTPS and public destination IPs are **required by default** for repository and HTTP
notification requests. Per-watch `allow_private_networks: true` permits internal
repositories; `allow_http: true` separately permits plain HTTP. HTTP notification
destinations have their own flags, useful for a local Signal service. TLS certificate
and hostname validation cannot be disabled. Environment HTTP proxies are ignored.
See [the security review](docs/security.md) for the policy and limitations.

## Docker

Docker also runs a single check and exits:

```sh
cp examples/sury.yml config.yml
# Set state_dir: /var/lib/gills in config.yml
docker compose build
docker compose run --rm gills check --dry-run
docker compose run --rm gills upstream check
docker compose run --rm gills prune --days 30
```

Schedule `docker compose run --rm gills check` with cron or a host timer if
preferred. There is no built-in scheduler, restart loop or polling daemon. The
container runs as UID 10001 with a persistent named volume. Make the mounted YAML
readable by this user. Enable `env_file: .env` in Compose for notification secrets.

SQLite and its process lock are intended for one host and local storage. Use one
state directory per independent installation; overlapping checks fail cleanly.

## Tests

```sh
poetry install --with dev
./tests.sh
poetry run ruff check src tests
poetry run ruff format --check src tests
```

Exit codes: `0` success, `1` a repository check or due delivery failed, `2`
configuration/usage/lock error, `130` interrupted. New releases alone are successful
checks. Configuration is validated automatically before either command.
