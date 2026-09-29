# Security model

## HTTPS / TLS

HTTPS is required by default for repository and HTTP notification URLs, including
URLs read from environment variables. Plain HTTP needs `allow_http: true` on the
particular watch or destination. Redirects from HTTPS to HTTP remain prohibited.

TLS verifies both the certificate chain and requested hostname. There is no
insecure TLS switch. Internal CAs can be installed in the system trust store or
supplied through Python's standard SSL_CERT_FILE/SSL_CERT_DIR environment settings.

## DNS / SSRF defenses

DNS answers are checked before connecting. Non-public IPv4/IPv6 addresses,
including LAN, loopback, link-local and shared-address ranges, are denied by
default. Mixed public/private DNS responses are rejected entirely. IPv4-mapped
IPv6 addresses receive the IPv4 checks; transition addresses are restricted.

Socket connections use the validated numeric address directly, without resolving
the hostname again. TLS still uses the original hostname for SNI and verification.
Every redirected connection repeats the address checks.

A watch may explicitly set `allow_private_networks: true` for an internal
repository. HTTP destinations have an independent opt-in for services such as
a local Signal API. This permits non-public addresses, including link-local;
use it only for trusted watches/services. Multicast, unspecified addresses and
6to4, Teredo and the standard NAT64 prefix remain prohibited.

Environment HTTP proxies are disabled: otherwise proxy-side resolution could
bypass application IP checks. SMTP uses its explicitly configured host; its
local-service support and explicit TLS modes are unchanged. SMTP TLS validates
certificates, and STARTTLS failures are not silently downgraded.

Cross-origin repository redirects discard custom headers. Notification POST
redirects are refused. A pre-existing redirect method lookup bug was corrected.
URL credentials, unsafe URL syntax, and custom routing/framing headers are rejected.

## Code execution / validation

Package archives are never downloaded, extracted or executed. Only metadata is
fetched, processed in memory, and compared with the SQLite baseline. No persistent
metadata cache is created.
Metadata paths must stay under the configured repository root and cannot supply
an independent URL, query, fragment or traversal path. Metadata SHA256 and declared
sizes are checked. Compressed and decompressed indexes have a configured byte limit.

RPM XML uses defusedxml; YAML uses safe_load and rejects unknown configuration keys.
SQLite writes use bound parameters and transactions. State-changing commands use a
process lock, and the CLI sets a restrictive file-creation umask.

## Secrets

Notification secrets are resolved at delivery time instead of being persisted in
batch payloads. Transport errors avoid exposing secret URLs or raw exception text.
Webhook HMAC and stable event/delivery identifiers support receiver verification
and deduplication. Slack repository text is sent as plain text blocks.

## Denial of service / data loss / resource exhaustion considerations

Pruning retains current baselines, waiting events and pending deliveries/batches.
It removes old completed SQLite history, then vacuums/checkpoints the database.
Legacy artifact/cache directories are untouched; no destructive file migration runs.

## Other limits and deployment assumptions

Configuration, environment variables, CA trust and the state directory are trusted
operator inputs. Anyone able to change them can change endpoints or opt out of
network restrictions. The application cannot infer unusual private routing for an
otherwise public address, network-specific NAT64 mappings, or a public relay that
forwards requests elsewhere. Host/container egress rules provide an additional boundary.

Repository cryptographic signature verification remains removed as requested.
HTTPS authenticates the server; index checksums detect inconsistent metadata but
do not establish authenticity against a compromised repository server.

Index limits apply per compressed/decompressed index, not to total process memory
or total execution time. Parsing many packages can use more memory than index size.
Socket timeouts do not impose a total transfer deadline; DNS uses OS resolver
behaviour. Apply service/container memory and runtime limits for untrusted large
repositories. Pending work intentionally survives pruning and may grow during a
long outage. Existing queued deliveries retain their original destinations when
watch routing changes; disable a destination to pause its pending retries.
