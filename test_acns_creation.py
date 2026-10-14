"""Creation and records-export contracts, without customer data or live writes."""

import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import acns_migrate as m
from test_acns_migrate import ACCOUNT, FakeAPI, fixture


def config():
    return {"account_id": ACCOUNT, "address_zones": ["example.com"],
            "registrar_domains": ["example.com"], "ttl": 3600,
            "sets": [{"ns_set": 1, "nameservers": ["ns01.example.com", "ns02.example.com"]},
                     {"ns_set": 2, "nameservers": ["ns03.example.com", "ns04.example.com"]}]}


class ProvisionAPI:
    """Deterministic responses for the portal-verified custom_ns GET/POST contract."""
    def __init__(self):
        self.nameservers = {}
        self.calls = []
        self.fail_name = None
        self.accept_then_fail_name = None
        self.pending_ips = False

    def add(self, name, number):
        offset = len(self.nameservers) + 10
        ns = {"ns_name": name, "ns_set": number, "status": "pending", "zone_tag": "b" * 32,
              "dns_records": [] if self.pending_ips else [
                  {"type": "A", "value": f"192.0.2.{offset}"},
                  {"type": "AAAA", "value": f"2001:db8::{offset}"},
              ]}
        self.nameservers[name] = ns
        return copy.deepcopy(ns)

    def result(self, method, path, *, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path != f"/accounts/{ACCOUNT}/custom_ns":
            raise AssertionError(f"Unexpected endpoint {path}")
        if method == "GET":
            return copy.deepcopy(list(self.nameservers.values()))
        if method != "POST" or set(body) != {"ns_name", "ns_set"}:
            raise AssertionError("Unexpected write or payload")
        name = body["ns_name"]
        if name in self.nameservers:
            raise AssertionError(f"Duplicate creation of {name}")
        if name == self.fail_name:
            raise m.MigrationError("Synthetic creation failure")
        result = self.add(name, body["ns_set"])
        if name == self.accept_then_fail_name:
            self.accept_then_fail_name = None
            raise m.MigrationError("Synthetic response lost after creation")
        return result


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "created.json"
        self.markdown = Path(str(self.output) + ".md")

    def run_create(self, api, value=None, **kwargs):
        with redirect_stdout(io.StringIO()):
            return m.create_nameservers(api, value or config(), self.output, **kwargs)

    def test_dry_run_only_lists_and_does_not_invent_addresses(self):
        api = ProvisionAPI()
        report = self.run_create(api)
        self.assertEqual([c[0] for c in api.calls], ["GET"])
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["pending_nameservers"]), 4)
        self.assertEqual(report["authoritative_records"], [])
        self.assertEqual(report["registrar_hosts"], [])
        self.assertIn("report incomplete", self.markdown.read_text())

    def test_creates_names_and_exports_exact_assigned_addresses(self):
        api = ProvisionAPI()
        report = self.run_create(api, execute=True)
        self.assertTrue(report["complete"])
        writes = [c for c in api.calls if c[0] == "POST"]
        self.assertEqual([c[2] for c in writes], [
            {"ns_name": "ns01.example.com", "ns_set": 1},
            {"ns_name": "ns02.example.com", "ns_set": 1},
            {"ns_name": "ns03.example.com", "ns_set": 2},
            {"ns_name": "ns04.example.com", "ns_set": 2},
        ])
        self.assertEqual(len(report["authoritative_records"]), 8)
        for record in report["authoritative_records"]:
            self.assertEqual(record["zone"], "example.com")
            self.assertFalse(record["proxied"])
            self.assertIn({"type": record["type"], "value": record["value"]},
                          api.nameservers[record["name"]]["dns_records"])
        host = report["registrar_hosts"][0]
        self.assertEqual(host, {"registered_domain": "example.com", "host": "ns01.example.com",
                                "ipv4": ["192.0.2.10"], "ipv6": ["2001:db8::10"]})
        self.assertIn("ns01.example.com. 3600 IN AAAA 2001:db8::10", self.markdown.read_text())
        self.assertEqual(m.load_json(self.output), report)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.markdown.stat().st_mode & 0o777, 0o600)

    def test_rerun_reuses_nameservers_without_any_new_posts(self):
        api = ProvisionAPI()
        self.run_create(api, execute=True)
        api.calls.clear()
        report = self.run_create(api, execute=True)
        self.assertTrue(report["complete"])
        self.assertTrue(all(c[0] == "GET" for c in api.calls))

    def test_partial_creation_is_reported_and_resumes(self):
        api = ProvisionAPI()
        api.fail_name = "ns02.example.com"
        with self.assertRaisesRegex(m.MigrationError, "Partial records saved"):
            self.run_create(api, execute=True)
        report = m.load_json(self.output)
        self.assertEqual(len(report["authoritative_records"]), 2)
        self.assertEqual(len(report["pending_nameservers"]), 3)
        api.fail_name = None
        report = self.run_create(api, execute=True)
        self.assertTrue(report["complete"])
        self.assertEqual(len([c for c in api.calls if c[0] == "POST" and c[2]["ns_name"] == "ns01.example.com"]), 1)

    def test_lost_post_response_is_reconciled_without_duplicate_post(self):
        api = ProvisionAPI()
        api.accept_then_fail_name = "ns01.example.com"
        report = self.run_create(api, execute=True)
        self.assertTrue(report["complete"])
        self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), 4)

    def test_existing_name_in_another_set_is_rejected_before_writes(self):
        api = ProvisionAPI()
        api.add("ns01.example.com", 3)
        with self.assertRaisesRegex(m.MigrationError, "another set"):
            self.run_create(api, execute=True)
        self.assertFalse(any(c[0] == "POST" for c in api.calls))

    def test_unexpected_set_member_is_rejected_before_writes(self):
        api = ProvisionAPI()
        api.add("other.example.com", 1)
        with self.assertRaisesRegex(m.MigrationError, "include all existing"):
            self.run_create(api, execute=True)
        self.assertFalse(any(c[0] == "POST" for c in api.calls))

    def test_pending_ips_are_reported_as_incomplete_and_rerun_does_not_recreate(self):
        api = ProvisionAPI()
        api.pending_ips = True
        with self.assertRaisesRegex(m.MigrationError, "assigned IPs are not yet available"):
            self.run_create(api, execute=True)
        report = m.load_json(self.output)
        self.assertFalse(report["complete"])
        self.assertTrue(all(ns["reason"] == "awaiting_assigned_ips" for ns in report["pending_nameservers"]))
        self.assertEqual(report["authoritative_records"], [])
        api.calls.clear()
        with self.assertRaises(m.MigrationError):
            self.run_create(api, execute=True)
        self.assertFalse(any(c[0] == "POST" for c in api.calls))

    def test_invalid_manifest_is_rejected_before_any_api_call(self):
        for kind in ("duplicate", "single", "six", "address_zone", "registrar", "set_number"):
            with self.subTest(kind=kind):
                value = config()
                if kind == "duplicate":
                    value["sets"][1]["nameservers"][0] = "NS01.EXAMPLE.COM."
                elif kind == "single":
                    value["sets"][0]["nameservers"].pop()
                elif kind == "six":
                    value["sets"][0]["nameservers"] = [f"ns{i}.example.com" for i in range(6)]
                elif kind == "address_zone":
                    value["address_zones"] = ["notexample.com"]
                elif kind == "registrar":
                    value["registrar_domains"] = ["example.net"]
                else:
                    value["sets"][0]["ns_set"] = 6
                api = ProvisionAPI()
                with self.assertRaises(m.MigrationError):
                    self.run_create(api, value, execute=True)
                self.assertEqual(api.calls, [])

    def test_output_cannot_overwrite_the_config(self):
        path = Path(self.temp.name) / "config.json"
        m.save_json(path, config())
        before = path.read_text()
        with patch.object(m, "CloudflareAPI") as client, redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(["create", "--config", str(path), "--output", str(path), "--execute"]), 1)
            client.assert_not_called()
        self.assertEqual(path.read_text(), before)

    def test_http_client_does_not_automatically_retry_creation_posts(self):
        class Opener:
            def __init__(self, error):
                self.error = error
                self.requests = []

            def open(self, req, timeout):
                self.requests.append(req)
                if self.error == "network":
                    raise URLError("Synthetic lost connection")
                raise HTTPError(req.full_url, self.error, "Synthetic failure", {}, io.BytesIO(b'{"errors":[]}'))

        for error in ("network", 429, 503):
            with self.subTest(error=error):
                opener = Opener(error)
                sleeps = []
                api = m.CloudflareAPI("SYNTHETIC_TOKEN", opener=opener, sleep=sleeps.append)
                with self.assertRaises(m.MigrationError):
                    api.result("POST", f"/accounts/{ACCOUNT}/custom_ns",
                               body={"ns_name": "ns01.example.com", "ns_set": 1})
                self.assertEqual(len(opener.requests), 1)
                self.assertEqual(sleeps, [])
                self.assertEqual(json.loads(opener.requests[0].data),
                                 {"ns_name": "ns01.example.com", "ns_set": 1})

    def test_creation_inventory_records_and_assignment_work_together(self):
        provision = ProvisionAPI()
        self.run_create(provision, execute=True)
        snapshot = fixture(["example.com", "app.example.com"], count=2)
        snapshot["sets"] = m.custom_sets(list(provision.nameservers.values()))
        api = FakeAPI(snapshot)
        collected = m.inventory(api, ACCOUNT)
        plan = m.plan_assignments(collected)
        records = m.export_plan_records(config(), plan)
        self.assertEqual(len(records["authoritative_records"]), 8)
        self.assertEqual(len(records["zones"][0]["delegation_glue_records"]), 4)
        self.assertEqual(records["zones"][1]["delegation_glue_records"], [])
        self.assertEqual([r["value"] for r in records["zones"][0]["apex_ns_records"]],
                         ["ns01.example.com", "ns02.example.com"])
        with redirect_stdout(io.StringIO()):
            m.apply_plan(api, plan, Path(self.temp.name) / "assignment-state.json")
        for actual, original, step in zip(api.snapshot["zones"], snapshot["zones"], plan["steps"]):
            self.assertEqual(actual["nameservers"], step["after"])
            self.assertEqual(actual["dnssec"], original["dnssec"])


class RecordExportTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = fixture(["example.net", "app.example.net"], count=2)
        for group in self.snapshot["sets"]:
            for i, ns in enumerate(group["nameservers"]):
                ns["ips"].append(f"2001:db8::{group['ns_set']}{i}")
                if group["ns_set"] == 2:
                    ns["name"] = ns["name"].replace(".example.net", ".app.example.net")
        self.plan = m.plan_assignments(self.snapshot)
        self.config = {"account_id": ACCOUNT, "address_zones": ["example.net", "app.example.net"],
                       "registrar_domains": ["example.net"], "ttl": 300,
                       "sets": [{"ns_set": s["ns_set"], "nameservers": [ns["name"] for ns in s["nameservers"]]}
                                for s in self.snapshot["sets"]]}

    def test_exports_apex_delegation_and_conditional_glue_with_correct_zone_context(self):
        report = m.export_plan_records(self.config, self.plan)
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["authoritative_records"]), 8)
        for record in report["authoritative_records"]:
            expected_zone = "app.example.net" if record["ns_set"] == 2 else "example.net"
            self.assertEqual(record["zone"], expected_zone)
        for zone in report["zones"]:
            self.assertEqual(len(zone["apex_ns_records"]), 2)
            self.assertEqual(zone["apex_ns_records"], zone["delegation_ns_records"])
            self.assertEqual(len(zone["delegation_glue_records"]), 4)
            self.assertEqual(zone["dnssec_action"], "retain_existing_ds")
            self.assertTrue(all(r["ttl"] == 300 for r in zone["apex_ns_records"]))
        self.assertEqual(report["zones"][1]["parent_zone_candidate"], "example.net")
        markdown = m.render_record_report(report)
        self.assertIn("app.example.net. 300 IN NS ns2a.app.example.net.", markdown)
        self.assertIn("retain existing Live Signing and parent DS", markdown)
        self.assertIn("actual delegating parent", markdown)

    def test_out_of_bailiwick_names_do_not_get_unnecessary_delegation_glue(self):
        snapshot = fixture(["example.com"], count=1)
        plan = m.plan_assignments(snapshot)
        value = {"account_id": ACCOUNT, "address_zones": ["example.net"],
                 "registrar_domains": ["example.net"], "sets": [
                     {"ns_set": 1, "nameservers": ["ns1a.example.net", "ns1b.example.net"]}]}
        report = m.export_plan_records(value, plan)
        self.assertEqual(report["zones"][0]["delegation_glue_records"], [])
        self.assertEqual(len(report["registrar_hosts"]), 2)

    def test_account_mismatch_or_uncovered_used_set_is_rejected(self):
        value = copy.deepcopy(self.config)
        value["account_id"] = "c" * 32
        with self.assertRaisesRegex(m.MigrationError, "different accounts"):
            m.export_plan_records(value, self.plan)
        value = copy.deepcopy(self.config)
        value["sets"].pop()
        with self.assertRaisesRegex(m.MigrationError, "include every used set"):
            m.export_plan_records(value, self.plan)

    def test_records_cli_is_offline_and_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, plan_path, output = root / "config.json", root / "plan.json", root / "records.json"
            m.save_json(config_path, self.config)
            m.save_json(plan_path, self.plan)
            with patch.object(m, "CloudflareAPI") as client, redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(["records", "--config", str(config_path), "--plan", str(plan_path),
                                         "--output", str(output)]), 0)
                client.assert_not_called()
            self.assertEqual(len(m.load_json(output)["zones"]), 2)
            self.assertTrue(Path(str(output) + ".md").is_file())


if __name__ == "__main__":
    unittest.main()
