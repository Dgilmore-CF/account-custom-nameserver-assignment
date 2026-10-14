# ACNS creation and bulk assignment for Secondary DNS

`acns_migrate.py` creates account custom nameservers, exports the DNS/registrar
records needed for cutover, inventories an account, and builds/applies a
conflict-free parent-first assignment plan. Python **3.10+**, macOS/Linux, and no
third-party Python dependencies are required.

## Strategy

1. Configure the desired nameserver sets and run `create`. Publish its generated
   authoritative address records and complete required host/glue registration.
2. Inventory **all account full and Secondary zones**, including existing
   assignments on zones outside the migration selection.
3. Plan the entire selected hierarchy before assigning anything, and run `records`
   to export the zone-specific apex NS, delegation, and required glue records.
4. Pilot one assignment, complete its record/delegation cutover, and verify DNS.
5. Resume the same reviewed plan and state, in controlled batches.

For disjoint sets and an initially standard-nameserver hierarchy, a typical plan is:

| Zone | Set |
| --- | --- |
| `example.com` | 1 |
| `app.example.com` | 2 |
| `api.app.example.com` | 3 |
| `test.example.com` | 2 |
| `example.net` | 1 |

Siblings and unrelated zones can reuse a set. Ancestors and descendants need
**disjoint custom nameserver IPs**, including across skipped hierarchy levels.
Nameserver labels such as `ns01.example.com` do not themselves create zone nodes.
Different set numbers with overlapping addresses are not independent sets.

The planner prefers retaining an eligible current assignment, then the lowest
eligible set number. It backtracks when an early choice makes a descendant
impossible. It also excludes a parent's choices that overlap **currently assigned
descendant IPs**, so the first parent-first write is executable, not just the
final arrangement. If no such plan exists, it stops rather than proposing an
invalid transition. Reordering alone cannot fix a fundamentally conflicting
hierarchy. An account supports at most five legacy sets, so a branch needing
six pairwise-disjoint custom assignments cannot fit this model.

## API contract and scope

Base URL: `https://api.cloudflare.com/client/v4`.

| Operation | Endpoint | Use |
| --- | --- | --- |
| GET | `/zones?account.id=…&page=…&per_page=50` | Paginated account inventory |
| GET | `/accounts/{account_id}/custom_ns` | Set membership and assigned A/AAAA addresses |
| POST | `/accounts/{account_id}/custom_ns` | Create one account custom nameserver |
| GET | `/zones/{zone_id}/dns_settings` | Existing nameserver configuration |
| GET | `/zones/{zone_id}/dnssec` | DNSSEC continuity snapshot |
| PATCH | `/zones/{zone_id}/dns_settings` | Nameserver assignment only |

The **entire PATCH body** is:

```json
{"nameservers":{"type":"custom.account","ns_set":1}}
```

Creation sends one request per missing name, with an explicit set number:

```json
{"ns_name":"ns01.example.com","ns_set":1}
```

The POST returns one nameserver object containing `ns_name`, `ns_set` (default
1 if omitted), and `dns_records` entries with `type: A|AAAA` and `value`.
The list endpoint returns an array and has no pagination parameters. Creation
reuses matching nameservers, rejects names in another set or unexpected extra
members in a requested set, and never deletes or moves existing nameservers.
POSTs are not automatically retried: after a failed/ambiguous response, creation
re-lists the account and reconciles the requested name before proceeding.

The tool targets the legacy **`custom.account` / `ns_set`** API. A related zone
using `custom`, `custom.tenant`, or `custom.zone` is rejected because its IPs
cannot be resolved from this account's legacy set inventory. Unrelated branches
with those configurations do not prevent planning.

The workflow follows Cloudflare's public [account custom nameserver configuration
conditions](https://developers.cloudflare.com/dns/nameservers/custom-nameservers/account-custom-nameservers/)
and the public API contract. This tool inventories full and Secondary zones for
assignment constraints and checks actual assigned IP overlap. Partial and
internal zones are outside its assignment scope.

Inventory requires access to **all zones in the account**, not just selected
targets. A scoped token that hides ancestor/descendant zones can produce an
incomplete view; Cloudflare's assignment API remains authoritative, including
for conflicts with zones in other accounts. Use account-wide Zone Read, DNS
Settings Read, DNSSEC Read, and account custom-nameserver read permissions. Creation
also needs account custom-nameserver write permission; assignment needs DNS
Settings Write on the targets. Permission labels can differ by account/API
entitlement; ensure each operation above is authorized. Creation itself only
calls the account custom-nameserver endpoint.

This tool preserves existing Secondary zone objects and stable Live Signing
configuration. It can also migrate unsigned zones while keeping DNSSEC disabled.
Presigned zones and pending/error DNSSEC states are rejected for selected targets.
Deprecated nameserver `status` is not used as a readiness signal: verify address
records and glue independently before applying.

## Usage

Run the commands from this directory. Supply the token via an environment variable
using your normal secret-management workflow; do not place it in a plan or command
argument. The variable name is **`CLOUDFLARE_API_TOKEN`**.

### 0. Configure and create the nameservers

Copy [acns-config.example.json](acns-config.example.json) to `acns-config.json`
and edit its account ID, nameserver names, sets, and zone/domain context:

```json
{
  "account_id": "REPLACE_WITH_32_CHARACTER_ACCOUNT_ID",
  "address_zones": ["example.com"],
  "registrar_domains": ["example.com"],
  "ttl": 3600,
  "sets": [
    {"ns_set": 1, "nameservers": ["ns01.example.com", "ns02.example.com"]},
    {"ns_set": 2, "nameservers": ["ns03.example.com", "ns04.example.com"]},
    {"ns_set": 3, "nameservers": ["ns05.example.com", "ns06.example.com"]}
  ]
}
```

- `address_zones`: actual authoritative zones hosting the nameservers' A/AAAA
  records. If a name matches multiple zones, the longest matching zone is used.
  For Secondary DNS, add records on that zone's hidden primary.
- `registrar_domains`: actual **registered domains**, used to identify the
  registrar managing each nameserver host. These may differ from address zones.
  Supply these explicitly; the tool does not guess registrations from suffixes
  such as `.co.uk` or from Cloudflare inventory relationships.
- `ttl`: suggested TTL for generated DNS snippets, default 3600. It does not
  change Cloudflare settings or registrar/registry-controlled TTLs.
- Each requested set must list its **complete intended membership**, with two
  to five names. Existing members are reused. Names in another set or unexpected
  extra members in a requested set stop creation before any writes. Other sets
  are left alone. Adding a member to an existing set can affect zones already
  using that set; include the intended complete membership.

Preview creation (GET requests only; local reports are written):

```bash
python3 acns_migrate.py create \
  --config acns-config.json \
  --output acns-created.json \
  --report acns-address-records.md
```

Create missing nameservers and export their Cloudflare-assigned records:

```bash
python3 acns_migrate.py create \
  --config acns-config.json \
  --output acns-created.json \
  --report acns-address-records.md \
  --execute
```

The JSON and Markdown reports contain:

- Exact authoritative `A` and `AAAA` records, grouped by the address zone where
  they belong, with `proxied: false` in JSON.
- Registrar host entries: registered domain, nameserver FQDN, assigned IPv4,
  and assigned IPv6 addresses, with conditional glue instructions.
- Any nameservers that have not been created or have no allocated IPs yet.

**New IPs cannot be known during a dry run.** No placeholder IPs are generated.
If a requested name has no assigned addresses after execution, the command exits
with an incomplete report; rerun when the addresses become available.

Re-running the same command re-lists the account and reuses completed names.
Reports are checkpointed after each creation so an interrupted run retains the
addresses already obtained. A failed/ambiguous POST is reconciled with GET; if
the requested name cannot be confirmed in the intended set, creation stops and
you can rerun after checking the account. The command never deletes nameservers,
reassigns existing names between sets, or patches zone/DNSSEC settings.

Publish/verify the generated authoritative addresses and required registrar
host/glue registrations before executing zone assignments. The registrar host
table provides registration details; it is not a blanket claim that every
out-of-bailiwick use needs glue. Actual child-delegation glue is also identified
in the zone-specific report below.

### 1. Read-only inventory

```bash
python3 acns_migrate.py inventory \
  --account-id YOUR_32_CHARACTER_ACCOUNT_ID \
  --output acns-inventory.json
```

This makes GET requests only. The snapshot includes set names/IPs, zone identity,
current nameserver settings, and raw DNSSEC metadata for Secondary zones.

### 2. Offline plan

```bash
python3 acns_migrate.py plan \
  --inventory acns-inventory.json \
  --sets 1,2,3 \
  --output acns-plan.json
```

Default zone selection: all **active Secondary zones**. Match `--sets` to the
set numbers in your creation config; omit it to consider every account set.
To limit the zone selection as well:

```bash
python3 acns_migrate.py plan \
  --inventory acns-inventory.json \
  --zones example.com,app.example.com,api.app.example.com \
  --sets 1,2,3 \
  --output acns-plan.json
```

Existing assignments on unselected zones remain constraints. The plan includes
each zone's old/new assignment, nameserver names/IPs, nearest known ancestor zone,
and DNSSEC baseline. The ancestor is an inventory relationship, not proof of where
the actual public delegation is managed. Review the JSON as well as the summary.
Plans are checksum-bound to their state files; regenerate rather than hand-edit.

### 3. Export each zone's required cutover records

```bash
python3 acns_migrate.py records \
  --config acns-config.json \
  --plan acns-plan.json \
  --output acns-cutover-records.json \
  --report acns-cutover-records.md
```

This is **offline** and includes all configured address/registrar records plus,
for every selected zone:

- Apex `NS` records to update on that zone's hidden primary.
- Separate public delegation `NS` records for the actual parent, or nameservers
  to set at the registrar when the zone is a registrable domain.
- `A/AAAA` glue records when a nameserver is in-bailiwick of that delegated zone.
  These go in the actual delegating parent, or through registrar/registry host
  registration for registrar-managed delegation.
- An instruction to retain existing Live Signing and parent DS (or keep DNSSEC
  disabled, if that was the baseline). No replacement DS is generated.

The nearest account ancestor is only a **parent candidate**, not proof of the
actual delegation boundary. Confirm placement before publishing delegation/glue
records, particularly if intermediate parents are outside this account. For a
registrable domain, the registrar's nameserver UI replaces editing parent NS
records directly. IPv6 rows are emitted only for addresses returned by Cloudflare.
The config must cover every set used by the plan. Run this export again after
generating a different plan; it uses that plan's snapshots, not live DNS.

### 4. Dry run

```bash
python3 acns_migrate.py apply --plan acns-plan.json
```

Without `--execute`, this prints the plan and makes **no API requests**. It does
not revalidate live readiness; the executing command performs live preflight.

### 5. Pilot one write

```bash
python3 acns_migrate.py apply \
  --plan acns-plan.json \
  --state acns-state.json \
  --max-zones 1 \
  --execute
```

This performs at most one new PATCH. Already-correct assignments are verified
without consuming the write limit. The first pending zone in parent-first order
is the pilot; review that choice in the plan.

After the pilot passes its full DNS cutover and validation, resume in batches:

```bash
python3 acns_migrate.py apply \
  --plan acns-plan.json \
  --state acns-state.json \
  --max-zones 25 \
  --execute
```

Omit `--max-zones` to finish all remaining assignments. If `--state` is omitted,
the default is `acns-plan.json.state.json`. Use the **same plan and state** for
resume; do not regenerate a plan between normal batches.

## Verification and resume behavior

- Before any writes, re-inventory account zones, nameserver settings, ACNS sets,
  and selected zones' DNSSEC. Stop on account topology/status changes, set/IP
  changes, unexpected assignments, or changed DNSSEC metadata.
- Read the target immediately before and after each assignment.
- Compare DNSSEC status, presigned/multi-signer/NSEC3 flags, public key, key tag,
  algorithm, digest, and DS metadata. Ignore `modified_on`, which is not key
  identity. The API snapshot exposes Cloudflare's KSK/DS metadata; it is not a
  complete audit of every ZSK or external multi-signer key.
- Persist `pending` **before** the PATCH and `done` only after verification.
  If interrupted after a successful PATCH, resume recognizes the desired live
  assignment, verifies DNSSEC, and records completion without another PATCH.
- Stop on the first failure. Do not automatically roll back assignments or keys.
  For DNSSEC drift, investigate before creating a new baseline.
- Retry GET/PATCH HTTP 429 and transient 5xx/network failures, up to five retries
  per request, honoring `Retry-After`. Assignments are idempotent. Creation POSTs
  use read-back reconciliation instead of automatic retries. A rejected hierarchy
  assignment is not retried with arbitrary alternate sets.
- Atomic JSON writes and a per-state-file advisory lock protect progress.
  macOS/Linux automatically releases the lock after process exit, including
  crashes. Avoid concurrent runs using different state files for the same zones.

Snapshots and state files contain operational metadata, not the token. They are
written with owner-only permissions. Network redirects are rejected so the bearer
token is not forwarded to an unexpected destination.

The preflight is not an atomic account transaction. Cloudflare can reject a write
if configuration changes concurrently or an unseen zone introduces a conflict.

## Complete the DNS cutover

Assignment is only the Cloudflare configuration step. For each migrated zone:

1. Update apex NS records on the hidden primary to the plan's nameserver names.
2. Increment SOA serial and transfer the updated zone to Cloudflare.
3. Query each assigned ACNS IP directly for authoritative NS/SOA/application
   responses and verify DNSSEC through a validating resolver.
4. Update registrar delegation for a registrable domain, or delegation records
   in the actual parent zone for a delegated child; transfer parent updates too.
5. Recheck public resolution, the delegation chain, and subsequent normal transfers.

Retain existing Live Signing and parent DS records for this assignment-only
migration, unless there is a separately coordinated signing-key change.

No primary record updates, forced AXFR requests, registrar changes, account
defaults, DNSSEC writes, or public DNS validation are performed by this tool.
Use the [Secondary DNS cutover guide](secondary-dns-cutover-guide.md) for those
steps, including direct authoritative queries and separate DNSSEC validation.

## Tests

```bash
python3 -m unittest -v
```

Tests use synthetic API responses and injected HTTP transport responses. No
customer account or live Cloudflare writes are involved. They verify hierarchy
and IP constraints, pagination, retries, dry runs, exact POST/PATCH payloads,
partial creation and response-loss reconciliation, address/glue/NS exports,
drift detection, partial runs, and crash-safe resume. These are local verification,
not proof of a production cutover or DNS propagation.
