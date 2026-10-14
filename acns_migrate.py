#!/usr/bin/env python3
"""Create account custom nameservers, export records, and plan/apply assignments.

Python 3.10+, standard library only. Network writes require --execute.
Writes: POST account custom_ns; PATCH zone dns_settings (nameservers only).
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_BASE = "https://api.cloudflare.com/client/v4"
SCHEMA_VERSION = 1
DNSSEC_FIELDS = (
    "status", "dnssec_presigned", "dnssec_multi_signer", "dnssec_use_nsec3",
    "algorithm", "digest", "digest_algorithm", "digest_type", "ds", "flags",
    "key_tag", "key_type", "public_key",
)


class MigrationError(Exception):
    """A failed API operation, precondition, or planning constraint."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise MigrationError(f"Invalid Cloudflare account/zone ID: {value!r}")
    return value


def dns_name(value):
    try:
        name = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except (AttributeError, UnicodeError) as exc:
        raise MigrationError(f"Invalid DNS name: {value!r}") from exc
    if len(name) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in name.split(".")
    ):
        raise MigrationError(f"Invalid DNS name: {value!r}")
    return name


def save_text(path, content):
    """Atomic, owner-readable persistence."""
    path = Path(path)
    if not path.parent.is_dir():
        raise MigrationError(f"Output directory does not exist: {path.parent}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    save_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def load_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


class CloudflareAPI:
    def __init__(self, token, *, retries=5, timeout=30, opener=None, sleep=time.sleep):
        if not token:
            raise MigrationError("Set CLOUDFLARE_API_TOKEN before a network command.")
        self.token = token
        self.retries = retries
        self.timeout = timeout
        self.opener = opener or build_opener(NoRedirects())
        self.sleep = sleep

    def request(self, method, path, *, query=None, body=None):
        url = API_BASE + path + ("?" + urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "acns-migrate/1",
        })
        # A creation POST may have succeeded even if its response was lost.
        # Reconcile it with GET rather than blindly sending another POST.
        retries = self.retries if method in ("GET", "PATCH") else 0
        for attempt in range(retries + 1):
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    payload = json.load(response)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                    try:
                        errors = json.load(exc).get("errors", [])
                        detail = "; ".join(
                            f"{e.get('code')}: {e.get('message')}" for e in errors
                        )[:500]
                    except (ValueError, AttributeError, TypeError):
                        detail = ""
                    finally:
                        exc.close()
                    raise MigrationError(
                        f"{method} {path}: HTTP {exc.code} {detail}"
                    ) from exc
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        delay = 2 ** attempt
                exc.close()
                self.sleep(max(0, delay))
                continue
            except (URLError, TimeoutError, ConnectionError) as exc:
                if attempt == retries:
                    raise MigrationError(f"{method} {path}: network request failed") from exc
                self.sleep(2 ** attempt)
                continue
            except ValueError as exc:
                raise MigrationError(f"{method} {path}: invalid JSON response") from exc
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise MigrationError(f"{method} {path}: unsuccessful Cloudflare response")
            if "result" not in payload:
                raise MigrationError(f"{method} {path}: missing result")
            return payload
        raise AssertionError("unreachable")

    def result(self, method, path, **kwargs):
        return self.request(method, path, **kwargs)["result"]


def account_zones(api, account_id):
    """Use the documented 50-zone page maximum; reject incomplete inventories."""
    zones, seen = [], set()
    page = 1
    while True:
        payload = api.request("GET", "/zones", query={
            "account.id": account_id, "page": page, "per_page": 50,
        })
        batch = payload["result"]
        if not isinstance(batch, list):
            raise MigrationError("Zone list result is not an array")
        for zone in batch:
            zone_id = identifier(zone["id"])
            if zone_id in seen or zone.get("account", {}).get("id") != account_id:
                raise MigrationError("Zone inventory changed or includes another account; retry inventory")
            seen.add(zone_id)
            zones.append(zone)
        info = payload.get("result_info", {})
        total_pages = info.get("total_pages")
        if total_pages is not None:
            if (isinstance(total_pages, bool) or not isinstance(total_pages, (int, float))
                    or total_pages < 0 or not float(total_pages).is_integer()):
                raise MigrationError("Invalid zone pagination metadata")
            finished = page >= total_pages
        else:
            finished = len(batch) < 50
        if finished:
            if info.get("total_count", len(zones)) != len(zones):
                raise MigrationError("Incomplete zone inventory; retry inventory")
            return zones
        if not batch:
            raise MigrationError("Empty page before the end of the zone inventory")
        page += 1


def index_nameservers(nameservers, *, require_ips=False):
    """Normalize API names/IPs; allow a partially created set during setup."""
    if not isinstance(nameservers, list):
        raise MigrationError("Custom nameserver result is not an array")
    indexed, counts = {}, {}
    for ns in nameservers:
        number = ns.get("ns_set", 1)
        if isinstance(number, bool) or number not in range(1, 6):
            raise MigrationError(f"Invalid account nameserver set: {number}")
        name = dns_name(ns["ns_name"])
        if name in indexed:
            raise MigrationError(f"Duplicate custom nameserver: {name}")
        ips = set()
        for record in ns["dns_records"]:
            try:
                address = ipaddress.ip_address(record["value"])
            except ValueError as exc:
                raise MigrationError(f"Invalid assigned IP address for {name}") from exc
            if record["type"] != ("A" if address.version == 4 else "AAAA"):
                raise MigrationError(f"Mismatched address record type for {name}")
            ips.add(str(address))
        if require_ips and not ips:
            raise MigrationError(f"No Cloudflare-assigned IP addresses for {name}")
        indexed[name] = {"ns_set": int(number), "name": name, "ips": sorted(ips)}
        counts[int(number)] = counts.get(int(number), 0) + 1
    if any(count > 5 for count in counts.values()):
        raise MigrationError("An account custom nameserver set exceeds five names")
    return indexed


def custom_sets(nameservers):
    grouped = {}
    for ns in index_nameservers(nameservers, require_ips=True).values():
        grouped.setdefault(ns["ns_set"], []).append({"name": ns["name"], "ips": ns["ips"]})
    result = []
    for number, members in sorted(grouped.items()):
        if not 2 <= len(members) <= 5:
            raise MigrationError(f"Set {number} needs two to five nameserver names")
        result.append({"ns_set": number, "nameservers": sorted(members, key=lambda n: n["name"])})
    if not result:
        raise MigrationError("No account custom nameserver sets found; create and prepare them first")
    return result


def containing_zone(name, zones, label):
    matches = [zone for zone in zones if name == zone or name.endswith("." + zone)]
    if not matches:
        raise MigrationError(f"No {label} configured for {name}")
    return max(matches, key=len)


def preparation_config(value):
    """Explicit zone/registrar context avoids guessing registered domains."""
    if not isinstance(value, dict):
        raise MigrationError("Creation config must be a JSON object")
    if value.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise MigrationError("Unsupported creation config schema version")
    config = {"schema_version": SCHEMA_VERSION, "account_id": identifier(value["account_id"])}
    for field in ("address_zones", "registrar_domains"):
        if not isinstance(value.get(field), list) or not value[field]:
            raise MigrationError(f"Config needs a non-empty {field} array")
        config[field] = sorted({dns_name(zone) for zone in value[field]})
    ttl = value.get("ttl", 3600)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 2147483647:
        raise MigrationError("Config ttl must be an integer between 1 and 2147483647")
    config["ttl"] = ttl
    if not isinstance(value.get("sets"), list) or not 1 <= len(value["sets"]) <= 5:
        raise MigrationError("Config needs one to five nameserver sets")
    sets, numbers, names = [], set(), set()
    for item in value["sets"]:
        if not isinstance(item, dict):
            raise MigrationError("Each configured set must be a JSON object")
        number = item["ns_set"]
        if isinstance(number, bool) or not isinstance(number, int) or number not in range(1, 6):
            raise MigrationError("Config ns_set must be an integer from 1 to 5")
        if number in numbers:
            raise MigrationError(f"Duplicate set number in config: {number}")
        numbers.add(number)
        members = item.get("nameservers")
        if not isinstance(members, list) or not 2 <= len(members) <= 5:
            raise MigrationError(f"Set {number} needs two to five nameserver names")
        normalized = []
        for member in members:
            name = dns_name(member)
            if "." not in name:
                raise MigrationError(f"Nameserver must be fully qualified: {name}")
            if name in names:
                raise MigrationError(f"Duplicate nameserver in config: {name}")
            names.add(name)
            containing_zone(name, config["address_zones"], "authoritative address zone")
            containing_zone(name, config["registrar_domains"], "registered domain")
            normalized.append(name)
        sets.append({"ns_set": number, "nameservers": sorted(normalized)})
    config["sets"] = sorted(sets, key=lambda s: s["ns_set"])
    return config


def check_creation_membership(config, available):
    desired = {s["ns_set"]: set(s["nameservers"]) for s in config["sets"]}
    for ns in available.values():
        if ns["ns_set"] in desired and ns["name"] not in desired[ns["ns_set"]]:
            raise MigrationError(
                f"Set {ns['ns_set']} already contains {ns['name']}; include all existing "
                "members in the config or use another set"
            )
    for number, names in desired.items():
        for name in names:
            if name in available and available[name]["ns_set"] != number:
                raise MigrationError(f"{name} already belongs to another set; creation will not move it")


def record_report(config, available, *, mode, plan=None):
    check_creation_membership(config, available)
    addresses, hosts, pending = [], [], []
    for group in config["sets"]:
        for name in group["nameservers"]:
            ns = available.get(name)
            if ns is None or not ns["ips"]:
                pending.append({"name": name, "ns_set": group["ns_set"],
                                "reason": "not_created" if ns is None else "awaiting_assigned_ips"})
                continue
            address_zone = containing_zone(name, config["address_zones"], "authoritative address zone")
            registered = containing_zone(name, config["registrar_domains"], "registered domain")
            ipv4, ipv6 = [], []
            for ip in ns["ips"]:
                kind = "AAAA" if ipaddress.ip_address(ip).version == 6 else "A"
                (ipv6 if kind == "AAAA" else ipv4).append(ip)
                addresses.append({"zone": address_zone, "name": name, "type": kind,
                                  "value": ip, "ttl": config["ttl"], "proxied": False,
                                  "ns_set": group["ns_set"]})
            hosts.append({"registered_domain": registered, "host": name,
                          "ipv4": ipv4, "ipv6": ipv6})
    report = {"schema_version": SCHEMA_VERSION, "account_id": config["account_id"],
              "generated_at": utc_now(), "mode": mode, "complete": not pending,
              "pending_nameservers": pending, "authoritative_records": addresses,
              "registrar_hosts": hosts, "zones": []}
    if plan is not None:
        check_plan(plan)
        if plan["account_id"] != config["account_id"]:
            raise MigrationError("Creation config and assignment plan belong to different accounts")
        if pending:
            raise MigrationError("Assignment plan is missing configured nameservers or their IP addresses")
        configured = {s["ns_set"]: set(s["nameservers"]) for s in config["sets"]}
        for step in plan["steps"]:
            number = step["after"]["ns_set"]
            if configured.get(number) != {ns["name"] for ns in step["nameservers"]}:
                raise MigrationError(f"Plan set {number} does not match the creation config; include every used set")
            zone = step["name"]
            ns_records, glue = [], []
            for ns in step["nameservers"]:
                ns_records.append({"name": zone, "type": "NS", "value": ns["name"], "ttl": config["ttl"]})
                if ns["name"] == zone or ns["name"].endswith("." + zone):
                    glue.extend({"name": ns["name"], "type": "AAAA" if ":" in ip else "A",
                                 "value": ip, "ttl": config["ttl"]} for ip in ns["ips"])
            report["zones"].append({"zone": zone, "ns_set": number,
                "parent_zone_candidate": step["parent_zone"], "apex_ns_records": ns_records,
                "delegation_ns_records": copy.deepcopy(ns_records), "delegation_glue_records": glue,
                "dnssec_action": "retain_existing_ds" if step["dnssec"]["status"] == "active" else "keep_disabled",
                "existing_ds": step["dnssec"].get("ds")})
        report["plan_id"] = plan["plan_id"]
    return report


def dns_record_line(record):
    value = record["value"] + "." if record["type"] == "NS" else record["value"]
    return f"{record['name']}. {record['ttl']} IN {record['type']} {value}"


def render_record_report(report):
    lines = ["# Account custom nameserver records", "",
             f"Account: `{report['account_id']}` · Mode: `{report['mode']}`", "",
             "TTL values below are configurable suggestions, not fetched DNS settings.", "",
             "## Authoritative address records", "",
             "Publish these DNS-only records in the indicated authoritative zones. For Secondary DNS,",
             "maintain them on the hidden primary, increment the SOA serial, and verify transfer.",
             "Reconcile/replace incorrect existing addresses; do not retain old provider IPs.", ""]
    for zone in sorted({r["zone"] for r in report["authoritative_records"]}):
        lines.extend([f"### Address zone: `{zone}`", "", "```dns"])
        lines.extend(dns_record_line(r) for r in report["authoritative_records"] if r["zone"] == zone)
        lines.extend(["```", ""])
    if report["pending_nameservers"]:
        lines.extend(["## Pending allocations — report incomplete", "",
                      "No placeholder IP addresses are generated. Create missing nameservers or rerun",
                      "after Cloudflare returns their assigned addresses before publishing records.", ""])
        lines.extend(f"- Set {ns['ns_set']}: `{ns['name']}` ({ns['reason']})"
                     for ns in report["pending_nameservers"])
        lines.append("")
    lines.extend(["## Registrar host / glue registration details", "",
                  "These are separate from authoritative A/AAAA records. Register host objects where",
                  "required by the registrar/registry, particularly for in-bailiwick registrar delegation.",
                  "For a delegated subzone, required glue belongs in its actual delegating parent zone.", "",
                  "| Registered domain | Nameserver host | IPv4 | IPv6 |",
                  "| --- | --- | --- | --- |"])
    lines.extend(f"| {h['registered_domain']} | {h['host']} | {', '.join(h['ipv4']) or '—'} | "
                 f"{', '.join(h['ipv6']) or '—'} |" for h in report["registrar_hosts"])
    lines.append("")
    if not report["zones"]:
        lines.extend(["## Zone NS and delegation records", "",
                      "After inventory and planning, run `records --config ... --plan ...` to export",
                      "the exact apex NS, public delegation, and conditional glue records for each zone.", ""])
    for zone in report["zones"]:
        lines.extend([f"## Zone: `{zone['zone']}` (set {zone['ns_set']})", "",
                      "### Apex NS — update this zone on its hidden primary", "", "```dns"])
        lines.extend(dns_record_line(r) for r in zone["apex_ns_records"])
        lines.extend(["```", "", "### Public delegation — separate parent/registrar update", ""])
        if zone["parent_zone_candidate"]:
            lines.append(f"Nearest account ancestor: `{zone['parent_zone_candidate']}`. Confirm that it is "
                         "the actual delegating parent before placing these records there.")
        else:
            lines.append("For a registrable domain, set these nameservers at its registrar. "
                         "For a delegated child, publish these records in its actual parent zone.")
        lines.extend(["", "```dns"])
        lines.extend(dns_record_line(r) for r in zone["delegation_ns_records"])
        lines.extend(["```", ""])
        if zone["delegation_glue_records"]:
            lines.extend(["### Required in-bailiwick delegation glue", "",
                          "Supply these addresses at the registrar/registry for registrar-managed",
                          "delegation, or as glue in the actual delegating parent for a child zone.", "", "```dns"])
            lines.extend(dns_record_line(r) for r in zone["delegation_glue_records"])
            lines.extend(["```", ""])
        if zone["dnssec_action"] == "retain_existing_ds":
            lines.extend(["DNSSEC: retain existing Live Signing and parent DS; this migration generates no new DS.", ""])
        else:
            lines.extend(["DNSSEC: leave disabled, matching the assignment baseline.", ""])
    lines.extend(["Publish address records and required host/glue registrations before assigning zones.",
                  "After assignment, update and transfer each zone's apex NS, verify authoritative responses",
                  "and DNSSEC at every assigned IP, then change public delegation and validate again.", ""])
    return "\n".join(lines)


def report_destinations(output, markdown, *, inputs=()):
    output = Path(output)
    markdown = Path(markdown) if markdown else Path(str(output) + ".md")
    paths = [output.resolve(), markdown.resolve(), *(Path(p).resolve() for p in inputs)]
    if len(set(paths)) != len(paths):
        raise MigrationError("Config, plan, JSON output, and Markdown report must use different paths")
    for path in (output, markdown):
        if not path.parent.is_dir():
            raise MigrationError(f"Output directory does not exist: {path.parent}")
    return output, markdown


def save_record_report(output, markdown, report):
    save_json(output, report)
    save_text(markdown, render_record_report(report))


def create_nameservers(api, value, output, markdown=None, *, execute=False):
    config = preparation_config(value)
    output, markdown = report_destinations(output, markdown)
    endpoint = f"/accounts/{config['account_id']}/custom_ns"
    with output.with_name(output.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError("Another process is using this creation output") from exc
        available = index_nameservers(api.result("GET", endpoint))
        check_creation_membership(config, available)

        def checkpoint():
            report = record_report(config, available, mode="execute" if execute else "dry_run")
            save_record_report(output, markdown, report)
            return report

        checkpoint()  # Confirm outputs are writable before the first API write.
        for group in config["sets"]:
            for name in group["nameservers"]:
                if name in available:
                    print(f"Reuse set {group['ns_set']}: {name}")
                    continue
                if not execute:
                    print(f"Would create set {group['ns_set']}: {name}")
                    continue
                # Re-read before each creation; a previous run or another writer
                # may already have created this name or changed set membership.
                available = index_nameservers(api.result("GET", endpoint))
                check_creation_membership(config, available)
                if name in available:
                    continue
                try:
                    result = api.result("POST", endpoint, body={"ns_name": name, "ns_set": group["ns_set"]})
                    created = index_nameservers([result])
                    if name not in created or created[name]["ns_set"] != group["ns_set"]:
                        raise MigrationError(f"Creation response does not match requested name/set: {name}")
                    available.update(created)
                except MigrationError as exc:
                    # A lost response or a racing create can be reconciled by
                    # name and set. Never blindly retry this POST.
                    try:
                        reconciled = index_nameservers(api.result("GET", endpoint))
                        check_creation_membership(config, reconciled)
                        available = reconciled
                    except MigrationError as reconcile_error:
                        checkpoint()
                        raise MigrationError(
                            f"Could not reconcile creation of {name}; partial records saved to {output}. "
                            "Rerun the same command after checking account nameservers."
                        ) from reconcile_error
                    checkpoint()
                    if name not in available or available[name]["ns_set"] != group["ns_set"]:
                        raise MigrationError(
                            f"Creation stopped at {name}: {exc}. Partial records saved to {output}; "
                            "rerun the same command to reuse completed nameservers."
                        ) from exc
                checkpoint()
                print(f"Created/verified set {group['ns_set']}: {name}")
        if execute:
            available = index_nameservers(api.result("GET", endpoint))
            check_creation_membership(config, available)
        report = checkpoint()
        if execute and not report["complete"]:
            raise MigrationError(f"Some nameservers or assigned IPs are not yet available; see {output} and rerun")
        return report


def export_plan_records(value, plan):
    config = preparation_config(value)
    check_plan(plan)
    raw = [{"ns_name": ns["name"], "ns_set": group["ns_set"],
            "dns_records": [{"type": "AAAA" if ":" in ip else "A", "value": ip} for ip in ns["ips"]]}
           for group in plan["sets"] for ns in group["nameservers"]]
    return record_report(config, index_nameservers(raw, require_ips=True), mode="assignment_plan", plan=plan)


def nameserver_setting(settings):
    ns = settings.get("nameservers", {})
    kind = ns.get("type")
    if kind in ("cloudflare.standard", "cloudflare.advanced"):
        return {"type": kind}
    if kind == "custom.account":
        number = ns.get("ns_set", 1)
        if isinstance(number, bool) or number not in range(1, 6):
            raise MigrationError(f"Invalid zone nameserver set: {number}")
        return {"type": kind, "ns_set": int(number)}
    # Kept in inventory; reject only if this node is on a selected zone's branch.
    return copy.deepcopy(ns)


def dnssec_snapshot(value):
    snapshot = {field: value.get(field) for field in DNSSEC_FIELDS}
    for field in ("dnssec_presigned", "dnssec_multi_signer", "dnssec_use_nsec3"):
        snapshot[field] = value.get(field, False)
        if not isinstance(snapshot[field], bool):
            raise MigrationError(f"Invalid DNSSEC flag: {field}")
    if snapshot["status"] not in ("active", "disabled"):
        raise MigrationError("DNSSEC must be stable (active or disabled) before planning/applying")
    if snapshot["dnssec_presigned"]:
        raise MigrationError("This Live Signing migration tool does not support presigned zones")
    if snapshot["status"] == "active" and any(
        snapshot[field] is None or snapshot[field] == ""
        for field in ("algorithm", "digest", "digest_type", "ds", "key_tag", "public_key")
    ):
        raise MigrationError("Active Live Signing zone is missing DNSSEC key/DS metadata")
    return snapshot


def inventory(api, account_id, *, dnssec_ids=None):
    identifier(account_id)
    sets = custom_sets(api.result("GET", f"/accounts/{account_id}/custom_ns"))
    zones = []
    for zone in account_zones(api, account_id):
        if zone["type"] not in ("full", "secondary"):
            continue
        zone_id = zone["id"]
        entry = {"id": zone_id, "name": dns_name(zone["name"]),
                 "type": zone["type"], "status": zone["status"],
                 "nameservers": nameserver_setting(api.result(
                     "GET", f"/zones/{zone_id}/dns_settings"))}
        # Raw DNSSEC is captured so planning a subset can skip presigned zones.
        if (dnssec_ids is None and zone["type"] == "secondary") or (
            dnssec_ids is not None and zone_id in dnssec_ids
        ):
            entry["dnssec"] = api.result("GET", f"/zones/{zone_id}/dnssec")
        zones.append(entry)
    return {"schema_version": SCHEMA_VERSION, "account_id": account_id,
            "created_at": utc_now(), "sets": sets,
            "zones": sorted(zones, key=lambda z: (z["name"], z["id"]))}


def tree(zones):
    by_name, children, parents = {}, {}, {}
    for zone in zones:
        name = dns_name(zone["name"])
        if name in by_name:
            raise MigrationError(f"Multiple zones named {name}; hierarchy is ambiguous")
        by_name[name] = zone
        children[name] = []
    for name in by_name:
        suffix = name.partition(".")[2]
        while suffix and suffix not in by_name:
            suffix = suffix.partition(".")[2]
        parents[name] = suffix or None
        if suffix:
            children[suffix].append(name)
    for siblings in children.values():
        siblings.sort()
    return by_name, children, parents


def set_ips(sets):
    return {s["ns_set"]: frozenset(
        ip for member in s["nameservers"] for ip in member["ips"]
    ) for s in sets}


def current_ips(zone, ips_by_set):
    ns = zone["nameservers"]
    if ns.get("type") in ("cloudflare.standard", "cloudflare.advanced"):
        return frozenset()
    if ns.get("type") != "custom.account" or ns.get("ns_set") not in ips_by_set:
        raise MigrationError(
            f"Cannot resolve existing custom nameserver IPs for {zone['name']} "
            f"({ns.get('type')}); plan this branch separately"
        )
    return ips_by_set[ns["ns_set"]]


def baseline(zones):
    return [{k: z[k] for k in ("id", "name", "type", "status", "nameservers")}
            for z in sorted(zones, key=lambda z: z["id"])]


def digest_plan(plan):
    content = {k: v for k, v in plan.items() if k != "plan_id"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_assignments(snapshot, *, selected=None, allowed_sets=None):
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise MigrationError("Unsupported inventory schema version")
    identifier(snapshot["account_id"])
    zones, sets = snapshot["zones"], snapshot["sets"]
    by_name, children, parents = tree(zones)
    targets = {z["name"] for z in zones if z["type"] == "secondary" and z["status"] == "active"}
    if selected is not None:
        selected = {dns_name(n) for n in selected}
        if selected - targets:
            raise MigrationError("Not active Secondary zones: " + ", ".join(sorted(selected - targets)))
        targets = selected
    if not targets:
        raise MigrationError("No active Secondary zones selected")
    ips_by_set = set_ips(sets)
    allowed = sorted(ips_by_set if allowed_sets is None else allowed_sets)
    if not allowed or set(allowed) - ips_by_set.keys():
        raise MigrationError("Requested nameserver sets are empty or missing from the account")
    security = {name: dnssec_snapshot(by_name[name]["dnssec"]) for name in targets}
    assignments = {}
    descendant_initial_ips = {}
    relevant = set()
    roots = set()
    for name in targets:
        node = name
        while node is not None:
            relevant.add(node)
            if parents[node] is None:
                roots.add(node)
            node = parents[node]
    pending = list(targets)
    visited = set()
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        relevant.add(node)
        pending.extend(children[node])

    def relevant_children(name):
        return [c for c in children[name] if c in relevant]

    def prepare(name):
        own = current_ips(by_name[name], ips_by_set)
        descendants = frozenset().union(*(prepare(c) for c in relevant_children(name)))
        descendant_initial_ips[name] = descendants
        return own | descendants

    def solve(name, ancestor_ips):
        zone = by_name[name]
        own = current_ips(zone, ips_by_set)
        if name in targets:
            candidates = sorted(allowed, key=lambda n: (n != zone["nameservers"].get("ns_set"), n))
            candidates = [n for n in candidates if not ips_by_set[n] & (
                ancestor_ips | descendant_initial_ips[name]
            )]
        else:
            candidates = [None] if not own & ancestor_ips else []
        for candidate in candidates:
            chosen = ips_by_set[candidate] if candidate is not None else own
            found = {name: candidate} if candidate is not None else {}
            for child in relevant_children(name):
                result = solve(child, ancestor_ips | chosen)
                if result is None:
                    break
                found.update(result)
            else:
                return found
        return None

    for root in sorted(roots):
        prepare(root)
        found = solve(root, frozenset())
        if found is None:
            raise MigrationError(
                f"No conflict-free parent-first plan for {root}. Ancestors and descendants "
                "need disjoint IPs; existing descendant assignments also constrain the "
                "first write. Prepare more disjoint sets or stage a separate transition."
            )
        assignments.update(found)

    def depth(name):
        return 0 if parents[name] is None else 1 + depth(parents[name])

    sets_by_number = {s["ns_set"]: s for s in sets}
    steps = []
    for name in sorted(targets, key=lambda n: (depth(n), n)):
        zone = by_name[name]
        number = assignments[name]
        after = {"type": "custom.account", "ns_set": number}
        steps.append({"zone_id": zone["id"], "name": name, "depth": depth(name),
                      "parent_zone": parents[name], "before": zone["nameservers"],
                      "after": after, "dnssec": security[name],
                      "action": "keep" if zone["nameservers"] == after else "assign",
                      "nameservers": sets_by_number[number]["nameservers"]})
    plan = {"schema_version": SCHEMA_VERSION, "account_id": snapshot["account_id"],
            "created_at": utc_now(), "sets": sets, "baseline": baseline(zones),
            "allowed_sets": allowed, "steps": steps}
    plan["plan_id"] = digest_plan(plan)
    return plan


def check_plan(plan):
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("plan_id") != digest_plan(plan):
        raise MigrationError("Unsupported or edited plan; generate a fresh plan")
    identifier(plan["account_id"])
    seen = set()
    for step in plan["steps"]:
        identifier(step["zone_id"])
        if step["zone_id"] in seen:
            raise MigrationError("Duplicate zone in plan")
        seen.add(step["zone_id"])
        dnssec_snapshot(step["dnssec"])
        if step["after"].get("type") != "custom.account":
            raise MigrationError("Plan includes a non-ACNS assignment")
    if not seen:
        raise MigrationError("Empty plan")


def preflight(api, plan, state):
    steps = {s["zone_id"]: s for s in plan["steps"]}
    live = inventory(api, plan["account_id"], dnssec_ids=set(steps))
    if live["sets"] != plan["sets"]:
        raise MigrationError("ACNS names/IPs changed since planning; create a new inventory and plan")
    expected = {z["id"]: z for z in plan["baseline"]}
    actual = {z["id"]: z for z in baseline(live["zones"])}
    if expected.keys() != actual.keys():
        raise MigrationError("Account zone inventory changed since planning; replan")
    for zone in live["zones"]:
        old, now = expected[zone["id"]], actual[zone["id"]]
        step = steps.get(zone["id"])
        if any(old[k] != now[k] for k in ("name", "type", "status")):
            raise MigrationError(f"Zone identity/type/status changed: {zone['name']}; replan")
        permitted = [step["before"], step["after"]] if step else [old["nameservers"]]
        if zone["nameservers"] not in permitted:
            raise MigrationError(f"Unexpected nameserver assignment: {zone['name']}; replan")
        if step:
            if dnssec_snapshot(zone["dnssec"]) != step["dnssec"]:
                raise MigrationError(f"DNSSEC key/DS/settings changed: {zone['name']}; investigate before resuming")
            if state["zones"].get(zone["id"], {}).get("status") == "done" and zone["nameservers"] != step["after"]:
                raise MigrationError(f"Previously completed assignment changed: {zone['name']}; replan")


def apply_plan(api, plan, state_path, *, max_zones=None):
    check_plan(plan)
    state_path = Path(state_path)
    if not state_path.parent.is_dir():
        raise MigrationError(f"State directory does not exist: {state_path.parent}")
    # POSIX advisory lock is released automatically even if the process crashes.
    with state_path.with_name(state_path.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError("Another process is using this state file") from exc
        state = load_json(state_path) if state_path.exists() else {
            "schema_version": SCHEMA_VERSION, "plan_id": plan["plan_id"], "zones": {},
        }
        if state.get("schema_version") != SCHEMA_VERSION or state.get("plan_id") != plan["plan_id"]:
            raise MigrationError("State belongs to a different plan; use a separate state file")
        preflight(api, plan, state)
        writes = 0
        for step in plan["steps"]:
            zone_id, name = step["zone_id"], step["name"]
            ns = nameserver_setting(api.result("GET", f"/zones/{zone_id}/dns_settings"))
            security = dnssec_snapshot(api.result("GET", f"/zones/{zone_id}/dnssec"))
            if security != step["dnssec"] or ns not in (step["before"], step["after"]):
                raise MigrationError(f"Pre-write settings/DNSSEC changed: {name}; stopped")
            if ns != step["after"]:
                if max_zones is not None and writes >= max_zones:
                    print(f"Paused after {writes} writes; resume with the same plan and state.")
                    break
                state["zones"][zone_id] = {"status": "pending", "updated_at": utc_now()}
                save_json(state_path, state)
                # Deliberately never includes DNSSEC, transfer, or other DNS settings.
                api.result("PATCH", f"/zones/{zone_id}/dns_settings", body={"nameservers": step["after"]})
                writes += 1
                ns = nameserver_setting(api.result("GET", f"/zones/{zone_id}/dns_settings"))
                security = dnssec_snapshot(api.result("GET", f"/zones/{zone_id}/dnssec"))
                if ns != step["after"] or security != step["dnssec"]:
                    raise MigrationError(f"Post-write assignment/DNSSEC verification failed: {name}; stopped")
            state["zones"][zone_id] = {"status": "done", "updated_at": utc_now()}
            save_json(state_path, state)
            print(f"Verified {name}: set {step['after']['ns_set']}, DNSSEC unchanged")
        return state


def print_plan(plan):
    check_plan(plan)
    print(f"Plan {plan['plan_id'][:12]}: {len(plan['steps'])} zones, "
          f"{sum(s['action'] == 'assign' for s in plan['steps'])} planned assignments")
    for step in plan["steps"]:
        print(f"  {step['action']:6} {step['name']} -> set {step['after']['ns_set']} "
              f"({', '.join(ns['name'] for ns in step['nameservers'])})")


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create missing account custom nameservers and export address/glue details")
    create.add_argument("--config", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path, help="JSON records report (also checkpointed during creation)")
    create.add_argument("--report", type=Path, help="Markdown report; default: OUTPUT_FILE.md")
    create.add_argument("--execute", action="store_true", help="Create missing names; default: GET-only preview")
    records = commands.add_parser("records", help="Export address, registrar, apex NS, and delegation records from a plan")
    records.add_argument("--config", required=True, type=Path)
    records.add_argument("--plan", required=True, type=Path)
    records.add_argument("--output", required=True, type=Path)
    records.add_argument("--report", type=Path, help="Markdown report; default: OUTPUT_FILE.md")
    collect = commands.add_parser("inventory", help="Read account sets, zones, DNS settings, and DNSSEC")
    collect.add_argument("--account-id", required=True)
    collect.add_argument("--output", required=True, type=Path)
    planner = commands.add_parser("plan", help="Build an offline, conflict-free parent-first plan")
    planner.add_argument("--inventory", required=True, type=Path)
    planner.add_argument("--output", required=True, type=Path)
    planner.add_argument("--zones", help="Comma-separated zone names; default: all active Secondary zones")
    planner.add_argument("--sets", help="Comma-separated existing set numbers; default: all account sets")
    apply = commands.add_parser("apply", help="Show the plan; add --execute to make assignments")
    apply.add_argument("--plan", required=True, type=Path)
    apply.add_argument("--execute", action="store_true")
    apply.add_argument("--state", type=Path, help="Default: PLAN_FILE.state.json")
    apply.add_argument("--max-zones", type=positive, help="Maximum new writes this run, e.g. 1 for a pilot")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            config = preparation_config(load_json(args.config))
            output, markdown = report_destinations(args.output, args.report, inputs=[args.config])
            api = CloudflareAPI(os.environ.get("CLOUDFLARE_API_TOKEN"))
            report = create_nameservers(api, config, output, markdown, execute=args.execute)
            print(render_record_report(report))
            print(f"Saved {output} and {markdown}")
            if not args.execute:
                print("Dry run: GET requests only. Add --execute to create missing nameservers and obtain their IPs.")
        elif args.command == "records":
            output, markdown = report_destinations(args.output, args.report, inputs=[args.config, args.plan])
            report = export_plan_records(load_json(args.config), load_json(args.plan))
            save_record_report(output, markdown, report)
            print(f"Saved {len(report['authoritative_records'])} address records and "
                  f"{len(report['zones'])} zone cutover record sets to {output} and {markdown}")
        elif args.command == "plan":
            plan = plan_assignments(load_json(args.inventory),
                selected=args.zones.split(",") if args.zones else None,
                allowed_sets={int(n) for n in args.sets.split(",")} if args.sets else None)
            save_json(args.output, plan)
            print_plan(plan)
            print(f"Saved {args.output}")
        elif args.command == "apply":
            plan = load_json(args.plan)
            print_plan(plan)
            if not args.execute:
                print("Dry run: no API requests. Add --execute to apply after reviewing the plan.")
                return 0
            api = CloudflareAPI(os.environ.get("CLOUDFLARE_API_TOKEN"))
            apply_plan(api, plan, args.state or Path(str(args.plan) + ".state.json"), max_zones=args.max_zones)
        else:
            api = CloudflareAPI(os.environ.get("CLOUDFLARE_API_TOKEN"))
            snapshot = inventory(api, args.account_id)
            save_json(args.output, snapshot)
            print(f"Saved {len(snapshot['zones'])} full/Secondary zones and "
                  f"{len(snapshot['sets'])} ACNS sets to {args.output}")
        return 0
    except (MigrationError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
