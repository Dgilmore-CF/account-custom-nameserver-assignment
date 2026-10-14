# ACNS cutover for Secondary DNS with a hidden primary

Use this runbook when existing Cloudflare Secondary DNS zones receive transfers
from a hidden primary and will use numbered account custom nameserver sets.
The hidden primary remains the record source; Cloudflare answers public queries.

## 1. Create and prepare account custom nameservers

Use `create` as described in the [tool guide](acns-migration-tool.md). Verify that
the generated address report is complete and uses Cloudflare-assigned addresses.
Each set needs two to five names, with no more than five numbered sets per account.

Publish each nameserver's assigned A/AAAA records in the authoritative zone that
hosts its name. For Secondary DNS, maintain these records on the hidden primary,
increment SOA serial, and verify transfer even if Cloudflare also creates records.
Keep the address records DNS-only.

Register nameserver host/glue details where required. For example, if
`ns01.example.com` serves `example.com`, the registry parent needs usable glue
to break the lookup dependency. Registrar host records and authoritative A/AAAA
records are separate. Out-of-bailiwick use does not inherently need parent glue.

## 2. Inventory and plan the hierarchy

Inventory all full and Secondary zones in the account, including assignments on
zones outside the intended migration. Use an account-wide view for planning.
Only active Secondary zones are assignment targets in this tool.

Different zones in a direct ancestor/descendant chain need eligible, disjoint
custom nameserver IPs. Siblings can reuse a set. Ordinary hostnames do not become
zone nodes merely because they contain dots. Review the complete plan before
applying assignments; existing descendants constrain the first parent-first write.

Run `records` against that plan to obtain:

- Nameserver address records, grouped by their authoritative zone.
- Registrar host details.
- Each selected zone's apex NS and public delegation NS records.
- Required in-bailiwick delegation glue.

Confirm the actual delegation boundary. The nearest ancestor in account inventory
is a candidate, not proof that it is the actual delegating parent.

## 3. Pilot an assignment

Use `apply --execute --max-zones 1` with a reviewed plan and persistent state file.
The assignment request changes only the zone's nameserver configuration. It does
not update transferred records, primary servers, or public delegation.

Keep the same zone object, account, Secondary type, transfer peers, and TSIG
configuration. Treat configuration, transfer, and delegation as separate changes;
verify the deployed endpoints before relying on old/new nameserver overlap.

## 4. Update and transfer the zone's apex NS records

On the hidden primary, replace that zone's public apex NS RRset with the names
from its assigned set. Do not publish the hidden primary as a public nameserver.
Increment SOA serial, publish/reload, and wait for a successful transfer.

An accepted transfer request alone is not evidence that updated records are served.
Changing a child's apex NS does not update its delegation records in the parent.

## 5. Verify the assigned nameservers directly

Use each assigned IPv4/IPv6 address from the generated report:

```bash
ACNS_IP="REPLACE_WITH_CLOUDFLARE_ASSIGNED_IP"
dig @"$ACNS_IP" example.com NS +norecurse
dig @"$ACNS_IP" example.com SOA +norecurse
dig @"$ACNS_IP" www.example.com A +norecurse
dig @"$ACNS_IP" example.com DNSKEY +dnssec +norecurse
```

Confirm authoritative responses (`aa`), the correct apex NS names and SOA serial,
and an application record that actually exists in the zone. Check each nameserver's
address records and signatures. Requesting DNSSEC records with `+dnssec` does not
itself validate them.

For an assignment-only migration of an existing Live Signing zone, retain the
signing configuration and parent DS when signing keys are unchanged. The tool
compares exposed DNSSEC key/DS/settings metadata before and after each assignment;
public DNS validation is a separate check. A provider/signing-key migration needs
its own DNSSEC procedure. Presigned or transitioning DNSSEC targets are rejected.

## 6. Update public delegation

- For a registrable domain, update its nameservers at the registrar. Cloudflare
  Registrar users should follow Cloudflare's documented Support process for ACNS.
- For a delegated child, update NS records in its actual parent zone and add glue
  there when the child's nameservers are in-bailiwick.
- If the parent is Secondary too, publish parent changes on its hidden primary,
  increment serial, and verify transfer.

After the change, inspect delegation and use a validating resolver or `delv`:

```bash
dig example.com NS +trace
dig app.example.com NS +trace
delv example.com A
```

Allow prior delegation/address TTLs to expire. Check normal application resolution
and verify that the next routine transfer preserves the intended records.

## 7. Resume and handle future zones

Resume with the same plan/state in controlled batches after the pilot passes.
If account topology, set IPs, assignments, or DNSSEC drift, investigate and create
a fresh inventory/plan when appropriate.

For eligible accounts, an account DNS default of `custom.account` affects new
zones only. It does not retrofit existing or revived zones. Read back a new zone's
actual assigned set before publishing NS/delegation; do not assume Set 1.
New Secondary zones need successful transfers and endpoint verification before
public delegation. Pending zones are outside this tool's assignment selection.

## References

- [Account custom nameservers](https://developers.cloudflare.com/dns/nameservers/custom-nameservers/account-custom-nameservers/)
- [Nameserver assignment and DNS zone defaults](https://developers.cloudflare.com/dns/nameservers/nameserver-options/)
- [Cloudflare as Secondary: setup](https://developers.cloudflare.com/dns/zone-setups/zone-transfers/cloudflare-as-secondary/setup/)
- [Secondary DNSSEC options](https://developers.cloudflare.com/dns/zone-setups/zone-transfers/cloudflare-as-secondary/dnssec-for-secondary/)
