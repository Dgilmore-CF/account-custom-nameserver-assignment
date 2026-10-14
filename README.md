# Account Custom Nameserver Assignment

Create Cloudflare account custom nameservers (ACNS), export the required DNS
and registrar records, and plan/apply hierarchy-aware assignments to existing
Secondary DNS zones.

**Python 3.10+ · macOS/Linux · standard library only**

This tool uses the numbered **`custom.account` / `ns_set`** configuration.
It does not manage the newer `custom` / `nameserver_set_id` API.

## What it does

- Creates missing account custom nameservers and reuses matching existing names.
- Exports assigned A/AAAA addresses and registrar host/glue details.
- Inventories account full and Secondary zones to detect hierarchy constraints.
- Plans conflict-free, parent-first assignments for active Secondary zones.
- Exports per-zone apex NS, public delegation NS, and required in-bailiwick glue.
- Checks DNSSEC metadata before/after assignment and supports resumable batches.

Creation and assignment require `--execute`. Creation previews use GET requests;
assignment previews and plan-based record exports are offline.

## Quick start

Copy [acns-config.example.json](acns-config.example.json) to `acns-config.json`.
Edit the account ID, nameserver names, set numbers, authoritative address zones,
and registered domains. Set `CLOUDFLARE_API_TOKEN` using your secret-management
workflow. The example's three sets accommodate a parent/child/grandchild example;
choose the sets needed for your own hierarchy.

```bash
# Preview creation; add --execute to create missing nameservers.
python3 acns_migrate.py create \
  --config acns-config.json \
  --output acns-created.json \
  --report acns-address-records.md

# After creation and address/glue preparation, collect current account state.
python3 acns_migrate.py inventory \
  --account-id YOUR_32_CHARACTER_ACCOUNT_ID \
  --output acns-inventory.json

# Use the set numbers configured for your account.
python3 acns_migrate.py plan \
  --inventory acns-inventory.json --sets 1,2,3 \
  --output acns-plan.json

# Export address records plus each selected zone's apex/delegation/glue records.
python3 acns_migrate.py records \
  --config acns-config.json --plan acns-plan.json \
  --output acns-cutover-records.json --report acns-cutover-records.md

# Offline assignment preview.
python3 acns_migrate.py apply --plan acns-plan.json
```

To pilot one new assignment after reviewing the plan:

```bash
python3 acns_migrate.py apply \
  --plan acns-plan.json --state acns-state.json \
  --max-zones 1 --execute
```

Complete that zone's primary-record update, transfer, authoritative DNS checks,
and public delegation cutover before resuming. Reuse the same plan/state and
adjust `--max-zones` for subsequent batches.

The tool creates ACNS names, not DNS zones. New-zone creation, transfer setup,
account DNS defaults, primary records, and registrar updates remain separate
operations. Full zones are inventoried as constraints, not assignment targets.

## Documentation

- [Complete usage and API contract](acns-migration-tool.md)
- [Secondary DNS cutover guide](secondary-dns-cutover-guide.md)
- [Blog: ACNS creation, assignment, and existing/new zones](blog-acns-creation-and-assignment.md)

## Tests

```bash
python3 -m unittest -v
```

The 37 local tests cover creation, response-loss reconciliation, record exports,
hierarchy/IP conflicts, pagination, retries, assignment, DNSSEC checks, and resume.
They use synthetic API responses, not a production account.

## Local operational files

The repository allowlists distributable files. Live configuration, inventory,
DNSSEC snapshots, progress state, and generated records stay outside version
control. The token is read from the environment and is not written to reports.
