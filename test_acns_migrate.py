"""Meaningful planning and migration lifecycle tests, with no live API calls."""

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import acns_migrate as m


ACCOUNT = "a" * 32
SECURITY = {
    "status": "active", "dnssec_presigned": False,
    "dnssec_multi_signer": False, "dnssec_use_nsec3": False,
    "algorithm": "13", "digest": "SYNTHETIC_DIGEST", "digest_type": "2",
    "ds": "example.com. 3600 IN DS 42 13 2 SYNTHETIC_DIGEST",
    "key_tag": 42, "public_key": "SYNTHETIC_PUBLIC_KEY",
}


def fixture(names, *, count=3):
    sets = [{"ns_set": n, "nameservers": [
        {"name": f"ns{n}a.example.net", "ips": [f"192.0.2.{2*n}"]},
        {"name": f"ns{n}b.example.net", "ips": [f"192.0.2.{2*n+1}"]},
    ]} for n in range(1, count + 1)]
    zones = [{"id": f"{i+1:032x}", "name": name, "type": "secondary",
              "status": "active", "nameservers": {"type": "cloudflare.standard"},
              "dnssec": copy.deepcopy(SECURITY)} for i, name in enumerate(names)]
    return {"schema_version": 1, "account_id": ACCOUNT, "sets": sets, "zones": zones}


class FakeAPI:
    """Synthetic responses matching the portal-verified public API shapes."""
    def __init__(self, snapshot, *, dnssec_drift=False, fail_after_write=False):
        self.snapshot = copy.deepcopy(snapshot)
        self.calls = []
        self.dnssec_drift = dnssec_drift
        self.fail_after_write = fail_after_write

    def request(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, query, copy.deepcopy(body)))
        if path == "/zones":
            start = (query["page"] - 1) * query["per_page"]
            zones = [{**z, "account": {"id": ACCOUNT}} for z in self.snapshot["zones"]]
            return {"success": True, "result": zones[start:start+query["per_page"]],
                    "result_info": {"total_count": len(zones),
                                    "total_pages": (len(zones) + 49) // 50}}
        if path == f"/accounts/{ACCOUNT}/custom_ns":
            nameservers = [{"ns_set": s["ns_set"], "ns_name": ns["name"],
                            "dns_records": [{"type": "AAAA" if ":" in ip else "A", "value": ip}
                                            for ip in ns["ips"]],
                            "status": "verified", "zone_tag": "b" * 32}
                           for s in self.snapshot["sets"] for ns in s["nameservers"]]
            return {"success": True, "result": nameservers}
        zone = next(z for z in self.snapshot["zones"] if z["id"] == path.split("/")[2])
        if path.endswith("/dnssec"):
            return {"success": True, "result": copy.deepcopy(zone["dnssec"])}
        if path.endswith("/dns_settings"):
            if method == "PATCH":
                zone["nameservers"] = copy.deepcopy(body["nameservers"])
                if self.dnssec_drift:
                    zone["dnssec"]["public_key"] = "CHANGED_KEY"
                if self.fail_after_write:
                    self.fail_after_write = False
                    raise m.MigrationError("Interrupted after server accepted PATCH")
            return {"success": True, "result": {"nameservers": copy.deepcopy(zone["nameservers"])}}
        raise AssertionError(f"Unexpected API endpoint: {method} {path}")

    def result(self, method, path, **kwargs):
        return self.request(method, path, **kwargs)["result"]


class PlanningTests(unittest.TestCase):
    def test_grandparent_conflict_and_sibling_reuse(self):
        snap = fixture(["example.com", "app.example.com", "api.app.example.com", "test.example.com", "example.org"])
        plan = m.plan_assignments(snap)
        assignments = {s["name"]: s["after"]["ns_set"] for s in plan["steps"]}
        self.assertEqual(assignments, {"example.com": 1, "app.example.com": 2,
            "api.app.example.com": 3, "test.example.com": 2, "example.org": 1})
        self.assertEqual(plan["steps"][0]["depth"], 0)
        self.assertEqual(plan["steps"][-1]["depth"], 2)

    def test_skipped_levels_and_dns_label_boundaries(self):
        snap = fixture(["example.com", "api.deep.example.com", "notexample.com"])
        plan = m.plan_assignments(snap)
        assignments = {s["name"]: s["after"]["ns_set"] for s in plan["steps"]}
        self.assertEqual(assignments["api.deep.example.com"], 2)
        self.assertEqual(assignments["notexample.com"], 1)

    def test_shared_ip_addresses_are_not_distinct_sets(self):
        snap = fixture(["example.com", "app.example.com"])
        snap["sets"][1]["nameservers"][0]["ips"] = ["192.0.2.2"]
        plan = m.plan_assignments(snap)
        self.assertEqual([s["after"]["ns_set"] for s in plan["steps"]], [1, 3])

    def test_equivalent_ipv6_addresses_are_normalized_before_planning(self):
        snap = fixture(["example.com", "app.example.com"])
        snap["sets"][0]["nameservers"][0]["ips"].append("2001:db8::1")
        snap["sets"][1]["nameservers"][0]["ips"].append("2001:0db8:0000:0000:0000:0000:0000:0001")
        normalized = m.inventory(FakeAPI(snap), ACCOUNT)
        plan = m.plan_assignments(normalized)
        self.assertEqual([s["after"]["ns_set"] for s in plan["steps"]], [1, 3])

    def test_six_level_branch_is_impossible_with_five_sets(self):
        snap = fixture(["example.com", "a.example.com", "b.a.example.com",
                        "c.b.a.example.com", "d.c.b.a.example.com", "e.d.c.b.a.example.com"], count=5)
        with self.assertRaisesRegex(m.MigrationError, "No conflict-free parent-first plan"):
            m.plan_assignments(snap)

    def test_existing_full_parent_constrains_selected_child(self):
        snap = fixture(["example.com", "app.example.com"])
        snap["zones"][0].update(type="full", nameservers={"type": "custom.account", "ns_set": 1})
        plan = m.plan_assignments(snap)
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["after"]["ns_set"], 2)

    def test_initial_descendant_constrains_first_parent_write(self):
        snap = fixture(["example.com", "app.example.com"])
        snap["zones"][1]["nameservers"] = {"type": "custom.account", "ns_set": 1}
        plan = m.plan_assignments(snap)
        self.assertEqual([s["after"]["ns_set"] for s in plan["steps"]], [2, 1])
        self.assertEqual(plan["steps"][1]["action"], "keep")

    def test_backtracking_finds_solution_with_fixed_descendant(self):
        # Set 1 overlaps both 2 and 3, but sets 2 and 3 are disjoint.
        # A fixed deep descendant uses 4, leaving a solution only after
        # backtracking from root 1 to root 2, then choosing child 3.
        snap = fixture(["example.com", "app.example.com", "deep.app.example.com"], count=4)
        snap["zones"][2]["nameservers"] = {"type": "custom.account", "ns_set": 4}
        snap["sets"][1]["nameservers"][0]["ips"] = ["192.0.2.2"]
        snap["sets"][2]["nameservers"][0]["ips"] = ["192.0.2.3"]
        plan = m.plan_assignments(snap, selected=["example.com", "app.example.com"])
        self.assertEqual([s["after"]["ns_set"] for s in plan["steps"]], [2, 3])

    def test_unsupported_unrelated_sibling_does_not_block(self):
        snap = fixture(["example.com", "app.example.com", "test.example.com"])
        snap["zones"][0]["type"] = "full"
        snap["zones"][2]["nameservers"] = {"type": "custom", "nameserver_set_id": "f" * 32}
        plan = m.plan_assignments(snap, selected=["app.example.com"])
        self.assertEqual(plan["steps"][0]["after"]["ns_set"], 1)
        snap["zones"][0]["type"] = "secondary"
        with self.assertRaisesRegex(m.MigrationError, "Cannot resolve"):
            m.plan_assignments(snap, selected=["example.com"])

    def test_pending_or_presigned_target_rejected(self):
        snap = fixture(["example.com"])
        snap["zones"][0]["dnssec"]["status"] = "pending"
        with self.assertRaisesRegex(m.MigrationError, "stable"):
            m.plan_assignments(snap)
        snap["zones"][0]["dnssec"].update(status="active", dnssec_presigned=True)
        with self.assertRaisesRegex(m.MigrationError, "presigned"):
            m.plan_assignments(snap)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state.json"
        self.snap = fixture(["example.com", "app.example.com"])
        self.plan = m.plan_assignments(self.snap)

    def test_inventory_paginates_at_contract_limit(self):
        api = FakeAPI(fixture([f"zone{i}.example.com" for i in range(101)]))
        snapshot = m.inventory(api, ACCOUNT)
        pages = [call[2] for call in api.calls if call[1] == "/zones"]
        self.assertEqual(len(snapshot["zones"]), 101)
        self.assertEqual([p["page"] for p in pages], [1, 2, 3])
        self.assertTrue(all(p["per_page"] == 50 for p in pages))

    def test_incomplete_and_repeated_zone_pages_rejected(self):
        api = FakeAPI(self.snap)
        with patch.object(api, "request", return_value={"success": True, "result": [],
            "result_info": {"total_count": 10, "total_pages": 1}}):
            with self.assertRaisesRegex(m.MigrationError, "Incomplete"):
                m.account_zones(api, ACCOUNT)
        duplicated = copy.deepcopy(self.snap)
        duplicated["zones"].append(copy.deepcopy(duplicated["zones"][0]))
        with self.assertRaisesRegex(m.MigrationError, "inventory changed"):
            m.account_zones(FakeAPI(duplicated), ACCOUNT)

    def test_pagination_accepts_integer_valued_spec_numbers(self):
        api = FakeAPI(self.snap)
        response = api.request("GET", "/zones", query={"page": 1, "per_page": 50})
        response["result_info"]["total_pages"] = 1.0
        with patch.object(api, "request", return_value=response):
            self.assertEqual(len(m.account_zones(api, ACCOUNT)), 2)
        response["result_info"]["total_pages"] = 1.5
        with patch.object(api, "request", return_value=response):
            with self.assertRaisesRegex(m.MigrationError, "pagination"):
                m.account_zones(api, ACCOUNT)

    def test_state_lock_prevents_concurrent_apply(self):
        lock_path = self.state.with_name(self.state.name + ".lock")
        with lock_path.open("a") as lock:
            m.fcntl.flock(lock, m.fcntl.LOCK_EX | m.fcntl.LOCK_NB)
            api = FakeAPI(self.snap)
            with self.assertRaisesRegex(m.MigrationError, "Another process"):
                m.apply_plan(api, self.plan, self.state)
            self.assertEqual(api.calls, [])

    def test_pilot_and_resume_write_exact_nameserver_payload_only(self):
        api = FakeAPI(self.snap)
        with redirect_stdout(io.StringIO()):
            state = m.apply_plan(api, self.plan, self.state, max_zones=1)
        self.assertEqual(len(state["zones"]), 1)
        with redirect_stdout(io.StringIO()):
            m.apply_plan(api, self.plan, self.state)
            m.apply_plan(api, self.plan, self.state)
        writes = [c for c in api.calls if c[0] == "PATCH"]
        self.assertEqual(len(writes), 2)
        self.assertEqual([c[3] for c in writes], [
            {"nameservers": {"type": "custom.account", "ns_set": 1}},
            {"nameservers": {"type": "custom.account", "ns_set": 2}},
        ])
        self.assertTrue(all(c[0] in ("GET", "PATCH") for c in api.calls))
        self.assertTrue(all(c[1].endswith("/dns_settings") for c in writes))
        self.assertEqual(len(m.load_json(self.state)["zones"]), 2)

    def test_resume_recognizes_patch_accepted_before_crash(self):
        api = FakeAPI(self.snap, fail_after_write=True)
        with self.assertRaisesRegex(m.MigrationError, "Interrupted"):
            m.apply_plan(api, self.plan, self.state)
        self.assertEqual(m.load_json(self.state)["zones"][self.snap["zones"][0]["id"]]["status"], "pending")
        with redirect_stdout(io.StringIO()):
            m.apply_plan(api, self.plan, self.state)
        self.assertEqual(len([c for c in api.calls if c[0] == "PATCH"]), 2)

    def test_post_write_dnssec_change_stops_before_next_zone(self):
        api = FakeAPI(self.snap, dnssec_drift=True)
        with self.assertRaisesRegex(m.MigrationError, "Post-write"):
            m.apply_plan(api, self.plan, self.state)
        self.assertEqual(len([c for c in api.calls if c[0] == "PATCH"]), 1)
        self.assertEqual(m.load_json(self.state)["zones"][self.snap["zones"][0]["id"]]["status"], "pending")
        with self.assertRaisesRegex(m.MigrationError, "DNSSEC key"):
            m.apply_plan(api, self.plan, self.state)

    def test_preflight_detects_topology_set_and_key_drift(self):
        for kind in ("topology", "set", "key"):
            with self.subTest(kind=kind):
                api = FakeAPI(self.snap)
                if kind == "topology":
                    api.snapshot["zones"].pop()
                elif kind == "set":
                    api.snapshot["sets"][0]["nameservers"][0]["ips"] = ["192.0.2.200"]
                else:
                    api.snapshot["zones"][0]["dnssec"]["key_tag"] = 43
                with self.assertRaises(m.MigrationError):
                    m.apply_plan(api, self.plan, self.state)
                self.assertFalse(any(c[0] == "PATCH" for c in api.calls))

    def test_edited_plan_and_state_from_another_plan_are_rejected(self):
        modified = copy.deepcopy(self.plan)
        modified["steps"][0]["after"]["ns_set"] = 3
        with self.assertRaisesRegex(m.MigrationError, "edited plan"):
            m.check_plan(modified)
        m.save_json(self.state, {"schema_version": 1, "plan_id": "other", "zones": {}})
        api = FakeAPI(self.snap)
        with self.assertRaisesRegex(m.MigrationError, "different plan"):
            m.apply_plan(api, self.plan, self.state)
        self.assertEqual(api.calls, [])

    def test_dry_run_never_constructs_api_client(self):
        path = Path(self.temp.name) / "plan.json"
        m.save_json(path, self.plan)
        with patch.object(m, "CloudflareAPI") as client, redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["apply", "--plan", str(path)]), 0)
            client.assert_not_called()

    def test_http_transport_honors_retry_after_and_no_auth_persistence(self):
        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, req, timeout):
                self.requests.append(req)
                if len(self.requests) == 1:
                    raise HTTPError(req.full_url, 429, "Rate limit", {"Retry-After": "3"}, io.BytesIO())
                return io.BytesIO(json.dumps({"success": True, "result": []}).encode())

        transport = Opener()
        sleeps = []
        api = m.CloudflareAPI("SYNTHETIC_TOKEN", opener=transport, sleep=sleeps.append)
        self.assertEqual(api.result("GET", "/zones", query={"page": 1}), [])
        self.assertEqual(sleeps, [3])
        self.assertEqual(transport.requests[0].full_url, m.API_BASE + "/zones?page=1")
        self.assertEqual(transport.requests[0].get_header("Authorization"), "Bearer SYNTHETIC_TOKEN")


if __name__ == "__main__":
    unittest.main()
