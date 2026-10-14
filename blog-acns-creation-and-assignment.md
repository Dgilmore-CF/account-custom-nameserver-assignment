---
title: "Automating Cloudflare Account Custom Nameservers: Creation, Assignment, and DNS Cutovers"
description: "A practical guide to ACNS, hierarchy-aware Secondary DNS migrations, required records, and nameserver defaults for new zones."
date: 2026-10-14
tags:
  - cloudflare
  - dns
  - dnssec
  - python
  - automation
---

# Automating Cloudflare Account Custom Nameservers

Using `ns01.example.com` and `ns02.example.com` instead of provider-branded
nameservers sounds like a small DNS change. Across a large account, it becomes
a coordination problem: nameserver creation, address records, parent/child zone
relationships, zone transfers, public delegation, and DNSSEC all have to agree.

I built **Account Custom Nameserver Assignment**, a Python tool that creates
Cloudflare account custom nameservers, exports the records needed to use them,
and plans resumable assignments for existing Secondary DNS zones.

The project is available at
[Dgilmore-CF/account-custom-nameserver-assignment](https://github.com/Dgilmore-CF/account-custom-nameserver-assignment).

The central idea is simple: **plan the whole hierarchy before changing individual
zones, and keep Cloudflare assignment separate from the DNS cutover.**

## What are account custom nameservers?

Account custom nameservers, or ACNS, let you define nameserver names at the
Cloudflare account level and reuse eligible sets across zones in that account.
Cloudflare continues providing authoritative DNS; the custom names provide the
identity published in DNS delegation.

For example, an organization might use:

```text
ns01.example.com
ns02.example.com
```

for multiple unrelated domains. This is useful for a consistent DNS identity,
centralized operations, and environments where record management remains on an
existing primary DNS platform while Cloudflare serves public DNS as a secondary.
The custom names do not mean you must operate public DNS servers yourself.

The domain providing the nameserver names does not have to be a zone in the same
Cloudflare account. Its authoritative provider must publish the correct address
records. That provider could be Cloudflare, an external DNS service, or a hidden
primary whose records transfer to Cloudflare.

Cloudflare supports custom nameservers with Primary/full and Secondary DNS setups.
ACNS availability depends on plan and enablement: the public documentation lists
Enterprise and Business after contacting Support. DNS zone defaults have their own
Enterprise availability. The script's **zone-assignment targets are active
Secondary zones**; it inventories full zones as hierarchy constraints.

### Numbered ACNS versus modern nameserver sets

This project implements the numbered ACNS configuration:

```json
{"nameservers":{"type":"custom.account","ns_set":1}}
```

That model supports up to five numbered sets, each containing two to five names,
with each name belonging to only one numbered set.

Cloudflare also documents a separate, newer nameserver-set API, currently in
Enterprise beta as of this article's date. It uses `type: "custom"` and a
`nameserver_set_id`, supports Advanced Custom Nameservers, and has different set
semantics. This script does not manage that API. In particular, the numbered-set
limits and account-default examples below refer to **`custom.account`**, not every
custom nameserver feature Cloudflare offers.

## The records that make ACNS work

There are several related configurations, and they serve different purposes.

| Configuration | Purpose | Where it is maintained |
| --- | --- | --- |
| Nameserver A/AAAA | Resolve each nameserver name to its assigned Cloudflare IPs | Authoritative zone hosting that name |
| Registrar host/glue | Bootstrap resolution when registrar-managed delegation needs in-bailiwick addresses | Registrar/registry host registration |
| Zone apex NS | Advertise the zone's authoritative nameserver names | Zone itself; hidden primary for Secondary DNS |
| Parent delegation NS | Direct resolvers from the parent into the child zone | Registrar for a registrable domain, or actual parent zone for a delegated child |
| Child-delegation glue | Supply in-bailiwick nameserver addresses across a child delegation | Actual delegating parent zone |

If `ns01.example.com` serves `example.com`, a resolver needs an address for that
nameserver before it can ask it about `example.com`. Parent glue breaks that
dependency. Authoritative A/AAAA records are still required; registrar host records
do not replace them.

For a delegated `app.example.com`, nameservers such as `ns01.app.example.com` need
glue in the actual delegating parent. Nameservers such as `ns03.example.com` are
outside the child's bailiwick and do not inherently require child-delegation glue.

Cloudflare may automatically create address records for legacy ACNS names hosted
on Cloudflare. The operational requirement is still to verify the right addresses
at the authoritative source. With Secondary DNS, maintain them on the hidden
primary so subsequent transfers preserve the intended records.

Use **Cloudflare's assigned ACNS IPs**, not the hidden primary's IPs or addresses
copied from old secondary nameservers. The tool exports the actual API-returned
addresses, with no invented IPs during dry runs.

## Why a large zone hierarchy needs a planner

Consider separately configured zones:

```text
example.com
├── app.example.com
│   └── api.app.example.com
└── test.example.com

example.net
```

A straightforward assignment for disjoint numbered sets might be:

| Zone | Set |
| --- | --- |
| `example.com` | 1 |
| `app.example.com` | 2 |
| `api.app.example.com` | 3 |
| `test.example.com` | 2 |
| `example.net` | 1 |

Siblings and unrelated zones can reuse a set. Zones in a direct ancestor/descendant
chain need compatible, non-overlapping custom nameserver assignments. Alternating
two sets is not sufficient for a parent, child, and grandchild.

The planner compares **assigned IPs**, not just set numbers. Different labels or
set numbers do not solve a conflict if their addresses overlap. It also considers
existing assignments on zones outside the selected migration.

These rules concern zone boundaries. An ordinary host such as `ns01.example.com`
does not need to become its own Cloudflare zone, and the names for several sets
can all have address records in `example.com`.

Parent-first order is useful only after the plan is feasible. An existing child's
current assignment can block the very first change to its parent. The planner
accounts for that intermediate state, prefers retaining eligible assignments,
and backtracks if an early choice leaves a descendant with no solution.

If the available sets cannot support a branch, the tool stops with a planning
error. A chain needing six pairwise-disjoint custom assignments cannot fit into
the five-numbered-set model.

## A practical workflow for existing Secondary DNS zones

The tool requires Python 3.10 or newer on macOS/Linux and uses only the standard
library. Start by cloning the repository and setting `CLOUDFLARE_API_TOKEN` through
your normal secret-management workflow.

```bash
git clone https://github.com/Dgilmore-CF/account-custom-nameserver-assignment.git
cd account-custom-nameserver-assignment
```

The token needs the account/zone read permissions used by inventory, account
custom-nameserver write permission for creation, and DNS settings write permission
for assignment. Inventory should see the entire account's relevant zones so
ancestors and descendants are not accidentally hidden from planning.

### 1. Describe the nameservers to create

Copy `acns-config.example.json` to `acns-config.json` and edit it. For example:

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

`address_zones` identifies the authoritative zones hosting the A/AAAA records.
`registrar_domains` identifies actual registered domains; the tool does not guess
registrations from labels or public suffixes. The TTL is a suggestion for record
snippets, not a change to Cloudflare or registry-controlled TTL settings.

### 2. Create ACNS names and export their addresses

Preview the operation:

```bash
python3 acns_migrate.py create \
  --config acns-config.json \
  --output acns-created.json \
  --report acns-address-records.md
```

This reads current nameservers and identifies missing names. Add `--execute` to
create them and obtain their assigned addresses:

```bash
python3 acns_migrate.py create \
  --config acns-config.json \
  --output acns-created.json \
  --report acns-address-records.md \
  --execute
```

The report groups exact A/AAAA records by authoritative zone and includes
registrar host details. Publish/verify those records and required glue before
assigning zones.

Matching nameservers are reused on subsequent runs. Unexpected existing set
members or names already in another set stop creation rather than causing a move
or deletion. Reports are checkpointed as creation progresses. If a POST response
is lost, the tool re-lists nameservers to reconcile the result instead of blindly
retrying the creation request.

### 3. Inventory the account and build a plan

```bash
python3 acns_migrate.py inventory \
  --account-id YOUR_32_CHARACTER_ACCOUNT_ID \
  --output acns-inventory.json

python3 acns_migrate.py plan \
  --inventory acns-inventory.json \
  --sets 1,2,3 \
  --output acns-plan.json
```

Inventory reads full and Secondary zones, current nameserver settings, account
sets, and Secondary DNSSEC metadata. Planning is offline. By default it selects
all active Secondary zones; use `--zones example.com,app.example.com` to select a
subset while retaining other zones as constraints.

### 4. Export the zone-specific records

```bash
python3 acns_migrate.py records \
  --config acns-config.json \
  --plan acns-plan.json \
  --output acns-cutover-records.json \
  --report acns-cutover-records.md
```

This offline report adds each selected zone's apex NS, public delegation NS,
and required in-bailiwick glue to the address/registrar information. It also
records the instruction to retain existing Live Signing and parent DS when
DNSSEC is active.

Confirm where delegation really lives. The nearest ancestor visible in the
account is only a parent candidate; an intermediate delegating zone may be
managed somewhere else.

### 5. Pilot one assignment, then complete its cutover

Preview assignment without making API requests:

```bash
python3 acns_migrate.py apply --plan acns-plan.json
```

Execute at most one new assignment:

```bash
python3 acns_migrate.py apply \
  --plan acns-plan.json \
  --state acns-state.json \
  --max-zones 1 \
  --execute
```

The executing command rechecks live inventory and DNSSEC before making changes.
It patches only the target's nameserver configuration and verifies the assignment
and exposed DNSSEC metadata afterward.

For a hidden-primary Secondary zone, finish the DNS cutover in this order:

1. Update that zone's apex NS RRset on the hidden primary.
2. Increment SOA serial and wait for the updated zone to transfer successfully.
3. Query every assigned ACNS IP directly for authoritative NS, SOA, application
   records, and DNSSEC data.
4. Update the registrar delegation or the NS/glue records in the actual parent.
5. Check public resolution and DNSSEC validation, then verify a routine transfer.

The hidden primary remains the record and transfer source; it stays out of public
delegation. A child zone's apex NS update does not update its parent's delegation.
If the parent is Secondary too, publish the delegation change on the parent's
hidden primary and verify its transfer.

Direct queries can use the report's actual IPs:

```bash
ACNS_IP="REPLACE_WITH_CLOUDFLARE_ASSIGNED_IP"
dig @"$ACNS_IP" example.com NS +norecurse
dig @"$ACNS_IP" example.com SOA +norecurse
dig @"$ACNS_IP" www.example.com A +norecurse
dig @"$ACNS_IP" example.com DNSKEY +dnssec +norecurse
```

Check the `aa` flag and expected content. `+dnssec` requests DNSSEC data; it is not
itself validation. After public delegation, validate the public path with a
validating resolver or a tool such as `delv`, and allow prior TTLs to expire.

Once the pilot is complete, resume the same reviewed plan/state with a larger
`--max-zones` value. Already-correct assignments are verified without consuming
the new-write limit. If interrupted after a successful PATCH, resume reads the
live assignment and DNSSEC before marking the step complete.

## What changes for new zones?

Creating account custom nameservers does not automatically move existing zones.
Eligible accounts can separately configure **DNS zone defaults** so newly created
zones receive ACNS assignments.

The account-level request is:

```http
PATCH /client/v4/accounts/{account_id}/dns_settings
```

with this body:

```json
{
  "zone_defaults": {
    "nameservers": {
      "type": "custom.account"
    }
  }
}
```

This affects assignment at new-zone creation time. It does **not** retrofit
existing zones or zones being revived after deletion. Cloudflare attempts Set 1
first and then other eligible sets in ascending order when conflicts occur.
Read back the actual assignment rather than assuming every new zone uses Set 1.

| Situation | Recommended approach |
| --- | --- |
| Existing active Secondary zone | Inventory, plan an explicit assignment, and coordinate its DNS cutover |
| New Secondary zone | Create/configure the zone and incoming transfer, inspect its actual assigned set, verify transferred records, then publish delegation |
| New Primary/full zone | Use supported ACNS/default configuration, prepare records, and update delegation; the script does not assign full zones |
| Previously deleted/revived zone | Inspect and configure its assignment explicitly; do not assume new-zone defaults apply |

For a new Secondary zone, successful transfer and authoritative verification
must precede public delegation. Cloudflare can answer for a newly created
Secondary zone before its first successful transfer, so delegating too early
can expose empty or negative answers.

The script does not create DNS zones, configure incoming transfer peers, or
change account DNS defaults. Its assignment planner also excludes pending zones;
use your supported provisioning process for initial onboarding. Once a Secondary
zone is active, it can be inventoried and included in a new plan for explicit
assignment or verification.

Account defaults do not enable DNSSEC. A genuinely new zone needs its normal
DNSSEC setup and DS publication if signing is desired. The newer
`nameserver_set_id` feature has separate behavior; current documentation says it
cannot yet be used as a DNS zone default.

## Preserving DNSSEC on existing zones

An ACNS assignment changes the nameserver configuration. For an existing
Cloudflare zone staying on the same account and Live Signing setup, changing
nameserver labels is not, by itself, a signing-key migration.

Retain the existing parent DS when signing keys remain unchanged. The tool reads
DNSSEC status, signing-mode flags, key tag, public key, algorithm, digest, and DS
metadata before and after assignment. Unexpected drift stops the run. It never
patches DNSSEC or generates a replacement DS.

Those API checks do not audit every ZSK or external multi-signer key, and they
do not replace DNS validation. Selected presigned zones and pending/error DNSSEC
states are rejected; stable unsigned zones remain unsigned. Moving between DNS
providers or changing signing keys needs a separate DNSSEC migration procedure.

## What is tested?

The project includes 37 local tests using synthetic API responses:

- ACNS creation, reuse, partial-run recovery, and response-loss reconciliation.
- Exact creation/assignment request bodies and assigned-address exports.
- Apex NS, delegation NS, and conditional glue reports.
- Parent/child/grandchild constraints, IP overlap, and existing assignments.
- Account pagination, retry handling, DNSSEC drift detection, and resumable apply.
- The complete creation → inventory → plan → records → assignment sequence.

Run them with:

```bash
python3 -m unittest -v
```

These tests establish local behavior, not production propagation or a guaranteed
zero-downtime cutover. Configuration changes, transfers, and delegation are
separate operations; live DNS checks remain part of the migration.

## Bringing it together

ACNS provides a consistent authoritative DNS identity across eligible zones.
Reliable automation connects that identity to the real account hierarchy,
assigned IPs, authoritative records, and public delegation.

For existing zones, make the assignment explicit and preserve the current zone
and DNSSEC baseline. For new zones, treat account defaults as an onboarding
policy, read back the actual assignment, and finish records and transfers before
delegation. In both cases, the generated records turn an API configuration into
something DNS resolvers can actually use.

## References

- [Project repository](https://github.com/Dgilmore-CF/account-custom-nameserver-assignment)
- [Cloudflare account custom nameservers](https://developers.cloudflare.com/dns/nameservers/custom-nameservers/account-custom-nameservers/)
- [Nameserver assignment and DNS zone defaults](https://developers.cloudflare.com/dns/nameservers/nameserver-options/)
- [Cloudflare nameserver sets](https://developers.cloudflare.com/dns/nameservers/custom-nameservers/nameserver-sets/)
- [Secondary DNS setup](https://developers.cloudflare.com/dns/zone-setups/zone-transfers/cloudflare-as-secondary/setup/)
- [DNSSEC options for Secondary DNS](https://developers.cloudflare.com/dns/zone-setups/zone-transfers/cloudflare-as-secondary/dnssec-for-secondary/)
- [Update zone DNS settings](https://developers.cloudflare.com/api/resources/dns/subresources/settings/subresources/zone/methods/edit/)
- [Update account DNS settings](https://developers.cloudflare.com/api/resources/dns/subresources/settings/subresources/account/methods/edit/)
