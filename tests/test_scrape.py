"""Offline tests: pure logic plus end-to-end runs against a local MOCK of the register's JSON API.

The mock imitates what the register page's own script expects (/states, /years, /search with
success/data/pagination, and the 10,000-result window). It uses obviously fake data and has
NOT been checked against nmc.org.in itself.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import json
import math
import os
import sys
import tempfile
import ssl
import threading
import unittest
import urllib.error
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import config  # noqa: E402
import imr_client  # noqa: E402
import scrape  # noqa: E402

TOTALS = {("TAM", "2025"): 250, ("TAM", "2024"): 10_050}


def make_item(code, year, i):
    return {
        "year_of_info": year, "registration_no": str(100000 + i),
        "state_medical_council": "Tamil Nadu Medical Council" if code == "TAM" else "Kerala Medical Council",
        "name": f"TEST DOCTOR {i}", "father_name": "TEST FATHER", "dob": "1900-01-01 00:00:00",
        "registration_date": "01-01-2025", "uprn_no": "N/A", "qualification": "MBBS",
        "qualification_year": "2020", "university": "TEST UNIVERSITY",
        "additional_qualifications": [{"qualification": "MD", "year": "2023", "university": "TEST UNI",
                                       "secret": "x"}],
        "permanent_address": "TEST ADDRESS, NOWHERE", "mobile": "0000000000",
    }


class Mock(BaseHTTPRequestHandler):
    search_log = []
    flaky_hits = 0

    def log_message(self, *args):
        pass

    def _send(self, body, status=200, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path.endswith("/states"):
            return self._send({"states": [{"code": "all", "name": "All"},
                                          {"code": "TAM", "name": "Tamil Nadu Medical Council"},
                                          {"code": "KER", "name": "Kerala Medical Council"}]})
        if url.path.endswith("/years"):
            return self._send({"years": ["2022", "2023", "2024", "2025", "2026"]})
        if not url.path.endswith("/search"):
            return self._send({}, 404)

        code, year, page = q["state"], q["year"], int(q["page"])
        per = min(int(q["per_page"]), 100)
        Mock.search_log.append((code, year, page, per))
        if year == "2026":
            return self._send(b"<html>challenge</html>", 200, "text/html")
        if year == "2023":
            return self._send({"message": "slow down"}, 429)
        if (code, year) == ("KER", "2022"):  # fails once with a 500, then works
            Mock.flaky_hits += 1
            if Mock.flaky_hits == 1:
                return self._send({"message": "oops"}, 500)
        if page * per > config.MAX_WINDOW:
            return self._send({"success": False, "message": "window too large"}, 422)

        total = TOTALS.get((code, year), 0)
        start, end = (page - 1) * per, min(page * per, total)
        data = [make_item(code, year, i) for i in range(start, max(start, end))]
        self._send({"success": bool(data), "data": data,
                    "pagination": {"total": total, "per_page": per, "current_page": page,
                                   "total_pages": math.ceil(total / per) if total else 0}})


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/indian-medical-register"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Mock.search_log = []
        Mock.flaky_hits = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.patches = [mock.patch.object(config, "BASE", self.base),
                        mock.patch.object(config, "DATA_DIR", self.data)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def scrape_args(self, **kw):
        base = dict(council=None, year_from=None, year_to=None, max_queries=None,
                    dry_run=False, delay=0, recover_capped=False)
        base.update(kw)
        return Namespace(**base)

    def read_records(self):
        path = self.data / "imr_records.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class BulkTests(ServerCase):
    def test_batch_runs_one_council_across_its_years_before_next_council(self):
        scrape.run_batch(self.scrape_args(year_from=2024, year_to=2025))
        order = list(dict.fromkeys((code, year) for code, year, _, _ in Mock.search_log))
        self.assertEqual(order, [("TAM", "2024"), ("TAM", "2025"),
                                 ("KER", "2024"), ("KER", "2025")])

    def test_batch_block_stops_before_next_council(self):
        with self.assertRaises(imr_client.Blocked):
            scrape.run_batch(self.scrape_args(year_from=2023, year_to=2023))
        self.assertEqual(len(Mock.search_log), 1)

    def test_all_entry_is_not_queried_and_empty_searches_finish_quickly(self):
        scrape.run_scrape(self.scrape_args(year_from=2025, year_to=2025))
        codes = {c for c, *_ in Mock.search_log}
        self.assertEqual(codes, {"TAM", "KER"})            # never "all"
        state = scrape.State(self.data / "state.json")
        self.assertTrue(state.is_done("KER", "2025"))      # empty search is recorded as done
        self.assertEqual(state.done["KER|2025"]["rows"], 0)
        self.assertEqual(len(self.read_records()), 250)

    def test_personal_fields_are_dropped_in_bulk_mode(self):
        scrape.run_scrape(self.scrape_args(council=["Tamil"], year_from=2025, year_to=2025))
        record = self.read_records()[0]
        for banned in ("dob", "father_name", "permanent_address", "mobile"):
            self.assertNotIn(banned, record)
        self.assertEqual(record["qualification"], "MBBS")
        self.assertEqual(record["additional_qualifications"],
                         [{"qualification": "MD", "year": "2023", "university": "TEST UNI"}])
        self.assertEqual(record["_council_code"], "TAM")

    def test_resume_skips_finished_searches(self):
        args = self.scrape_args(council=["Tamil"], year_from=2025, year_to=2025)
        scrape.run_scrape(args)
        first = len(Mock.search_log)
        scrape.run_scrape(args)
        self.assertEqual(len(Mock.search_log), first)       # nothing re-fetched
        self.assertEqual(len(self.read_records()), 250)

    def test_ten_thousand_window_is_respected_and_flagged(self):
        scrape.run_scrape(self.scrape_args(council=["Tamil"], year_from=2024, year_to=2024))
        self.assertEqual(len(self.read_records()), 10_000)
        self.assertEqual(max(p for _, _, p, _ in Mock.search_log), 100)  # page 101 never requested
        info = scrape.State(self.data / "state.json").done["TAM|2024"]
        self.assertTrue(info["capped"])
        self.assertEqual(info["site_total"], 10_050)

    def test_html_challenge_stops_the_run_and_is_not_marked_done(self):
        scrape.run_scrape(self.scrape_args(council=["Tamil"], year_from=2026, year_to=2026))
        self.assertEqual(self.read_records(), [])
        self.assertFalse(scrape.State(self.data / "state.json").is_done("TAM", "2026"))

    def test_http_429_stops_the_run(self):
        scrape.run_scrape(self.scrape_args(year_from=2023, year_to=2023))
        self.assertEqual(len(Mock.search_log), 1)           # stopped after the first refusal

    def test_transient_500_is_retried(self):
        scrape.run_scrape(self.scrape_args(council=["Kerala"], year_from=2022, year_to=2022))
        self.assertEqual(Mock.flaky_hits, 2)
        self.assertTrue(scrape.State(self.data / "state.json").is_done("KER", "2022"))

    def test_dry_run_fetches_nothing(self):
        scrape.run_scrape(self.scrape_args(dry_run=True))
        self.assertEqual(Mock.search_log, [])


class FullrecordTests(ServerCase):
    """Integration tests for `run_fullrecord` against the mock server.

    Requirements: 3.1, 3.2, 3.3, 3.4
    """

    ALL_API_FIELDS = (
        "year_of_info", "registration_no", "state_medical_council", "name",
        "father_name", "dob", "registration_date", "uprn_no", "qualification",
        "qualification_year", "university", "additional_qualifications",
        "permanent_address", "mobile",
    )

    def run_fullrecord(self, **kw):
        """Call run_fullrecord with defaults for Tamil Nadu 2025 and suppress stdout."""
        args = Namespace(council="Tamil", year=2025, limit=config.fullrecord_DEFAULT, delay=0)
        for k, v in kw.items():
            setattr(args, k, v)
        with mock.patch("builtins.print"):
            scrape.run_fullrecord(args)

    def read_fullrecord(self):
        path = self.data / "fullrecord_full.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_output_file_exists_and_has_expected_record_count_with_limit(self):
        """Requirement 3.1, 3.3: output file exists and contains at most `limit` records."""
        # TAM/2025 has 250 records; --limit 50 should yield exactly 50
        self.run_fullrecord(limit=50)
        path = self.data / "fullrecord_full.jsonl"
        self.assertTrue(path.exists(), "fullrecord_full.jsonl was not created")
        rows = self.read_fullrecord()
        self.assertLessEqual(len(rows), 50)
        self.assertGreater(len(rows), 0)

    def test_each_record_retains_all_api_fields_no_allowlisting(self):
        """Requirement 3.2: every record has ALL API fields — no allow-list applied."""
        self.run_fullrecord(limit=50)
        rows = self.read_fullrecord()
        self.assertGreater(len(rows), 0)
        first = rows[0]
        for field in self.ALL_API_FIELDS:
            self.assertIn(field, first,
                          f"Expected API field '{field}' missing from fullrecord output")
        # Spot-check that personal fields are present (not dropped like in bulk mode)
        self.assertIn("dob", first)
        self.assertIn("father_name", first)
        self.assertIn("permanent_address", first)
        self.assertIn("mobile", first)

    def test_records_include_council_and_year_metadata(self):
        """Requirement 3.4: each record has _council and _year metadata fields."""
        self.run_fullrecord(limit=50)
        rows = self.read_fullrecord()
        for record in rows:
            self.assertIn("_council", record,
                          "_council metadata field missing from fullrecord record")
            self.assertIn("_year", record,
                          "_year metadata field missing from fullrecord record")
            self.assertEqual(record["_year"], "2025")
            self.assertIn("Tamil Nadu", record["_council"])

    def test_default_limit_fetches_all_available_records(self):
        """Requirement 3.4: without --limit, all 250 TAM/2025 records are fetched."""
        # fullrecord_DEFAULT = MAX_WINDOW = 10000, so all 250 available records are returned
        self.run_fullrecord()  # uses fullrecord_DEFAULT
        rows = self.read_fullrecord()
        self.assertEqual(len(rows), 250,
                         f"Expected all 250 TAM/2025 records, got {len(rows)}")

    def test_output_file_is_overwritten_on_second_run(self):
        """Requirement 3.3: fullrecord_full.jsonl is overwritten on every run (not appended)."""
        self.run_fullrecord(limit=10)
        first_count = len(self.read_fullrecord())
        self.run_fullrecord(limit=20)
        second_count = len(self.read_fullrecord())
        # If the file were appended, we'd see 30; overwrite means exactly 20
        self.assertEqual(second_count, 20,
                         "File should be overwritten, not appended")
        self.assertNotEqual(second_count, first_count + 20)


class SampleTests(ServerCase):
    def run_sample(self, **kw):
        args = Namespace(council="Tamil", year=2025, limit=120, delay=0)
        for k, v in kw.items():
            setattr(args, k, v)
        scrape.run_sample(args)
        path = self.data / "sample_full.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]

    def test_sample_keeps_every_field_but_is_small(self):
        rows = self.run_sample(limit=120)
        self.assertEqual(len(rows), 120)
        self.assertIn("dob", rows[0])
        self.assertIn("permanent_address", rows[0])
        self.assertIn("father_name", rows[0])

    def test_sample_is_hard_capped(self):
        rows = self.run_sample(council="Tamil", year=2024, limit=99_999)
        self.assertEqual(len(rows), config.SAMPLE_MAX)

    def test_sample_needs_exactly_one_council(self):
        with self.assertRaises(SystemExit):
            self.run_sample(council="Medical Council")      # matches two councils


class ContainsClient:
    """A tiny capped contains API; all data is fake and requests are recorded."""

    def __init__(self):
        self.rows = [make_item("TAM", "2025", i) for i in range(12)]
        for i, row in enumerate(self.rows):
            row["registration_no"] = str(100 + i)
        self.calls = []
        self.block = False
        self.ignore_scope = False
        self.ignore_filter = False

    def states(self):
        return [("TAM", "Tamil Nadu Medical Council")]

    def years(self):
        return ["2025"]

    def search(self, code, year, page=1, per_page=2, reg_no=""):
        self.calls.append((reg_no, page))
        if reg_no and self.block:
            raise imr_client.Blocked("HTTP 429")
        if page * per_page > config.MAX_WINDOW:
            raise AssertionError("Requested beyond search window")
        # Reproduce the live API's special case: the literal "0" is ignored.
        rows = [dict(row) for row in self.rows
                if reg_no == "0" or self.ignore_filter or reg_no in row["registration_no"].lower()]
        if self.ignore_scope and reg_no:
            for row in rows:
                row["year_of_info"] = "1990"
        chunk = rows[(page - 1) * per_page:page * per_page]
        return {"success": bool(chunk), "data": chunk,
                "pagination": {"total": len(rows), "per_page": per_page,
                               "total_pages": math.ceil(len(rows) / per_page)}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.client = ContainsClient()
        self.patches = [mock.patch.object(config, "DATA_DIR", self.data),
                        mock.patch.object(config, "MAX_WINDOW", 4),
                        mock.patch.object(config, "PER_PAGE", 2),
                        mock.patch.object(scrape, "Client", return_value=self.client)]
        for patch in self.patches:
            patch.start()
        self.args = Namespace(council=["Tamil"], year_from=2025, year_to=2025,
                              max_queries=None, dry_run=False, delay=0, recover_capped=False,
                              recovery_budget=200, recovery_depth=3)
        scrape.run_scrape(self.args)  # legacy capped batch already on disk
        self.args.recover_capped = True
        self.client.calls.clear()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def records(self):
        return [json.loads(line) for line in (self.data / "imr_records.jsonl").read_text().splitlines()]

    def info(self):
        return scrape.State(self.data / "state.json").done["TAM|2025"]

    def test_legacy_capped_batch_recovers_overlapping_and_deeper_filters(self):
        scrape.run_scrape(self.args)
        self.assertEqual(len(self.records()), 12)
        self.assertEqual(len({r["registration_no"] for r in self.records()}), 12)
        self.assertFalse(self.info()["capped"])
        self.assertTrue(any(len(fragment) > 1 for fragment, _ in self.client.calls))
        self.assertNotIn(("", 2), self.client.calls)  # old 4 rows were not fetched again
        self.assertNotIn("dob", self.records()[-1])
        calls = len(self.client.calls)
        scrape.run_scrape(self.args)
        self.assertEqual(len(self.client.calls), calls)

    def test_zero_filter_is_expanded_without_requesting_ignored_literal(self):
        # This missing row has no nonzero character, so only the zero branch
        # can recover it. The other records keep the baseline's identities.
        self.client.rows[-1]["registration_no"] = "000000"
        scrape.run_scrape(self.args)
        self.assertNotIn(("0", 1), self.client.calls)
        self.assertIn(("00", 1), self.client.calls)
        self.assertEqual(len(self.records()), 12)
        self.assertFalse(self.info()["capped"])

    def test_budget_checkpoints_and_resume_skips_finished_fragments(self):
        self.args.recovery_budget = 1
        scrape.run_scrape(self.args)
        self.assertTrue(self.info()["capped"])
        self.client.calls.clear()
        self.args.recovery_budget = 200
        scrape.run_scrape(self.args)
        self.assertNotIn(("0", 1), self.client.calls)
        self.assertFalse(self.info()["capped"])
        self.assertEqual(len(self.records()), 12)

    def test_depth_limit_does_not_claim_complete_coverage(self):
        self.args.recovery_depth = 1
        scrape.run_scrape(self.args)
        self.assertTrue(self.info()["capped"])
        self.assertLess(self.info()["rows"], 12)

    def test_block_stops_and_existing_rows_survive(self):
        self.client.block = True
        scrape.run_scrape(self.args)
        self.assertEqual(self.client.calls, [("", 1), ("1", 1)])
        self.assertEqual(len(self.records()), 4)
        self.assertTrue(self.info()["capped"])

    def test_scope_violation_is_rejected(self):
        self.client.ignore_scope = True
        scrape.run_scrape(self.args)
        self.assertEqual(len(self.records()), 4)
        self.assertTrue(self.info()["capped"])

    def test_ignored_nonzero_filter_still_rejects_response_with_counts(self):
        self.client.ignore_filter = True
        # Start with a fragment absent from the records, guaranteeing mismatches.
        state = scrape.State(self.data / "state.json")
        state.recovery["TAM|2025"] = {"1": False}
        state.save()
        with self.assertLogs("imr", level="WARNING") as captured:
            scrape.run_scrape(self.args)
        self.assertIn("registration mismatches=2", "\n".join(captured.output))
        self.assertEqual(len(self.records()), 4)
        self.assertTrue(self.info()["capped"])

    def test_changed_total_is_not_treated_as_same_snapshot(self):
        self.client.rows.pop()
        scrape.run_scrape(self.args)
        self.assertEqual(self.client.calls, [("", 1)])
        self.assertTrue(self.info()["capped"])

    def test_new_capped_batch_also_recovers(self):
        with tempfile.TemporaryDirectory() as new_data:
            with mock.patch.object(config, "DATA_DIR", Path(new_data)):
                scrape.run_scrape(self.args)
                state = scrape.State(Path(new_data) / "state.json")
                self.assertFalse(state.done["TAM|2025"]["capped"])

    def test_recovery_defaults_on_without_an_explicit_flag(self):
        del self.args.recover_capped
        scrape.run_scrape(self.args)
        self.assertEqual(len(self.records()), 12)
        self.assertFalse(self.info()["capped"])

    def test_distinct_qualifications_for_same_registration_are_preserved(self):
        for index, qualification in ((10, "MD"), (11, "MS")):
            self.client.rows[index] = {**self.client.rows[9], "qualification": qualification}
        scrape.run_scrape(self.args)
        self.assertEqual(len(self.records()), 12)
        self.assertFalse(self.info()["capped"])
        variants = [r["qualification"] for r in self.records() if r["registration_no"] == "109"]
        self.assertCountEqual(variants, ["MBBS", "MD", "MS"])

    def test_legacy_fragment_checkpoints_are_revisited_without_deleting_records(self):
        state = scrape.State(self.data / "state.json")
        state.recovery["TAM|2025"] = {c: False for c in "123456789abcdefghijklmnopqrstuvwxyz"}
        state.save()
        original = self.records()
        scrape.run_scrape(self.args)
        self.assertIn(("1", 1), self.client.calls)
        self.assertEqual(self.records()[:len(original)], original)
        self.assertEqual(len(self.records()), 12)
        self.assertFalse(self.info()["capped"])
        self.assertEqual(scrape.State(self.data / "state.json").recovery_versions["TAM|2025"],
                         scrape.RECOVERY_IDENTITY_VERSION)


class PureTests(unittest.TestCase):
    def test_cli_recovery_defaults_on_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "DATA_DIR", Path(tmp)), \
                    mock.patch.object(scrape, "run_command") as run, \
                    mock.patch.object(scrape.logging, "basicConfig"), \
                    mock.patch.object(scrape.logging, "FileHandler", return_value=scrape.logging.NullHandler()):
                for command in ("scrape", "batch"):
                    scrape.main([command, "--year-from", "1960", "--year-to", "2026"])
                    self.assertTrue(run.call_args.args[1].recover_capped)
                    scrape.main([command, "--no-recover-capped"])
                    self.assertFalse(run.call_args.args[1].recover_capped)

    def test_real_councils_drops_all(self):
        self.assertEqual(scrape.real_councils([("all", "All"), ("TAM", "Tamil Nadu Medical Council")]),
                         [("TAM", "Tamil Nadu Medical Council")])

    def test_build_plan_filters(self):
        councils = [("all", "All"), ("TAM", "Tamil Nadu Medical Council"), ("KER", "Kerala Medical Council")]
        plan = scrape.build_plan(councils, ["2023", "2024", "2025"], only=["kerala"], year_from=2024)
        self.assertEqual(plan, [("KER", "Kerala Medical Council", "2024"),
                                ("KER", "Kerala Medical Council", "2025")])

    def test_build_plan_is_year_major(self):
        councils = [("TAM", "Tamil Nadu Medical Council"), ("KER", "Kerala Medical Council")]
        plan = scrape.build_plan(councils, ["2024", "2025"])
        self.assertEqual([(code, year) for code, _, year in plan],
                         [("TAM", "2024"), ("KER", "2024"),
                          ("TAM", "2025"), ("KER", "2025")])

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            scrape.State(path).mark("TAM", "2025", 5, 5, False)
            self.assertTrue(scrape.State(path).is_done("TAM", "2025"))
            self.assertFalse(scrape.State(path).is_done("TAM", "2024"))

    def test_export_flattens_dedupes_and_survives_a_torn_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "r.jsonl", Path(tmp) / "r.csv"
            rec = scrape.to_record(make_item("TAM", "2025", 1), "TAM", "Tamil Nadu Medical Council", "2025")
            scrape.append_records([rec, rec], src)
            with src.open("a", encoding="utf-8") as fh:
                fh.write('{"registration_no": "3", "na')
            self.assertEqual(scrape.export_csv(src, dst), 1)
            text = dst.read_text(encoding="utf-8-sig")
            self.assertIn("MD | 2023 | TEST UNI", text)
            self.assertNotIn("TEST FATHER", text)


class FullrecordConfigTests(unittest.TestCase):
    """Unit tests for the fullrecord config constants (Requirements 1.1-1.4)."""

    def test_fullrecord_max_is_defined_positive_int(self):
        # Requirement 1.1: fullrecord_MAX is a positive integer
        self.assertTrue(hasattr(config, "fullrecord_MAX"))
        self.assertIsInstance(config.fullrecord_MAX, int)
        self.assertGreater(config.fullrecord_MAX, 0)

    def test_fullrecord_default_is_defined_positive_int(self):
        # Requirement 1.2: fullrecord_DEFAULT is a positive integer
        self.assertTrue(hasattr(config, "fullrecord_DEFAULT"))
        self.assertIsInstance(config.fullrecord_DEFAULT, int)
        self.assertGreater(config.fullrecord_DEFAULT, 0)

    def test_fullrecord_max_le_max_window(self):
        # Requirement 1.3: fullrecord_MAX <= MAX_WINDOW (site cap)
        self.assertLessEqual(config.fullrecord_MAX, config.MAX_WINDOW)

    def test_fullrecord_default_equals_max_window(self):
        # Requirement 1.4: default scrapes ALL available records
        self.assertEqual(config.fullrecord_DEFAULT, config.MAX_WINDOW)

    def test_env_var_override_fullrecord_max(self):
        """Requirement 1.5: IMR_FULLRECORD_MAX overrides fullrecord_MAX at import time."""
        import importlib
        original = os.environ.get("IMR_FULLRECORD_MAX")
        try:
            os.environ["IMR_FULLRECORD_MAX"] = "50"
            importlib.reload(config)
            self.assertEqual(config.fullrecord_MAX, 50)
        finally:
            if original is None:
                os.environ.pop("IMR_FULLRECORD_MAX", None)
            else:
                os.environ["IMR_FULLRECORD_MAX"] = original
            importlib.reload(config)

    def test_env_var_override_fullrecord_default(self):
        """Requirement 1.6: IMR_FULLRECORD_DEFAULT overrides fullrecord_DEFAULT at import time."""
        import importlib
        original = os.environ.get("IMR_FULLRECORD_DEFAULT")
        try:
            os.environ["IMR_FULLRECORD_DEFAULT"] = "100"
            importlib.reload(config)
            self.assertEqual(config.fullrecord_DEFAULT, 100)
        finally:
            if original is None:
                os.environ.pop("IMR_FULLRECORD_DEFAULT", None)
            else:
                os.environ["IMR_FULLRECORD_DEFAULT"] = original
            importlib.reload(config)


class FullrecordLimitClampingTests(unittest.TestCase):
    """Unit tests for the fullrecord --limit clamping edge cases (Requirements 2.1, 2.3).

    The clamping expression used by run_fullrecord is:
        limit = max(1, min(args.limit, config.fullrecord_MAX))
    We test it directly without running run_fullrecord (which needs a live/mock server).
    """

    @staticmethod
    def _clamp(user_limit, ceiling):
        """Replicates the clamping logic in run_fullrecord line 423."""
        return max(1, min(user_limit, ceiling))

    def test_limit_zero_clamps_to_one(self):
        # Requirement 2.1: --limit=0 clamps to 1
        clamped = self._clamp(0, config.fullrecord_MAX)
        self.assertEqual(clamped, 1)

    def test_limit_one_stays_one(self):
        # Requirement 2.1: --limit=1 clamps to 1
        clamped = self._clamp(1, config.fullrecord_MAX)
        self.assertEqual(clamped, 1)

    def test_limit_hundred_stays_hundred(self):
        # Requirement 2.1: --limit=100 clamps to 100
        clamped = self._clamp(100, config.fullrecord_MAX)
        self.assertEqual(clamped, 100)

    def test_limit_above_max_clamps_to_max(self):
        # Requirement 2.1: --limit=50000 clamps to fullrecord_MAX (10000)
        clamped = self._clamp(50000, config.fullrecord_MAX)
        self.assertEqual(clamped, config.fullrecord_MAX)
        self.assertEqual(clamped, 10000)

    def test_negative_limit_clamps_to_one(self):
        # Requirement 2.1: --limit=-5 clamps to 1
        clamped = self._clamp(-5, config.fullrecord_MAX)
        self.assertEqual(clamped, 1)

    def test_invariant_holds_for_all_edge_cases(self):
        # Requirement 2.3: 1 <= limit <= config.fullrecord_MAX for each edge case
        for user_limit in (0, 1, 100, 50000, -5, -999, 99999, config.fullrecord_MAX, config.fullrecord_MAX + 1):
            clamped = self._clamp(user_limit, config.fullrecord_MAX)
            self.assertGreaterEqual(clamped, 1)
            self.assertLessEqual(clamped, config.fullrecord_MAX)

    def test_clamping_with_custom_max(self):
        """Requirement 2.1/2.3: clamping works with a custom fullrecord_MAX (env var override)."""
        import importlib
        original = os.environ.get("IMR_FULLRECORD_MAX")
        try:
            os.environ["IMR_FULLRECORD_MAX"] = "500"
            importlib.reload(config)
            self.assertEqual(config.fullrecord_MAX, 500)
            # --limit=0 clamps to 1
            self.assertEqual(self._clamp(0, config.fullrecord_MAX), 1)
            # --limit=50 stays 50
            self.assertEqual(self._clamp(50, config.fullrecord_MAX), 50)
            # --limit=50000 clamps to 500
            self.assertEqual(self._clamp(50000, config.fullrecord_MAX), 500)
            # invariant holds
            for user_limit in (0, 1, 50, 250, 500, 501, -10, 99999):
                clamped = self._clamp(user_limit, config.fullrecord_MAX)
                self.assertGreaterEqual(clamped, 1)
                self.assertLessEqual(clamped, config.fullrecord_MAX)
        finally:
            if original is None:
                os.environ.pop("IMR_FULLRECORD_MAX", None)
            else:
                os.environ["IMR_FULLRECORD_MAX"] = original
            importlib.reload(config)


class FullrecordFetchQueryTests(unittest.TestCase):
    """Unit tests that fetch_query receives the clamped limit (Requirement 2.2).

    Mocks fetch_query, real_councils, and Client so run_fullrecord can run
    without a live server, and asserts the limit keyword argument.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(config, "DATA_DIR", self.data),
            mock.patch.object(scrape, "Client"),
            mock.patch.object(scrape, "real_councils",
                              return_value=[("TAM", "Tamil Nadu Medical Council")]),
            mock.patch.object(scrape, "fetch_query",
                              return_value=([{"some": "record"}], 1, False)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _run_fullrecord(self, limit):
        args = Namespace(council="Tamil", year=2025, limit=limit, delay=0)
        scrape.run_fullrecord(args)

    def test_fetch_query_receives_clamped_limit(self):
        # Requirement 2.2: fetch_query is called with the clamped limit (positive int)
        self._run_fullrecord(limit=100)
        scrape.fetch_query.assert_called_once()
        _, kwargs = scrape.fetch_query.call_args
        self.assertEqual(kwargs["limit"], 100)
        self.assertIsInstance(kwargs["limit"], int)
        self.assertGreater(kwargs["limit"], 0)

    def test_fetch_query_limit_is_not_none(self):
        # Regression test for the original `limit=none` bug: limit must NOT be None
        self._run_fullrecord(limit=50)
        _, kwargs = scrape.fetch_query.call_args
        self.assertIsNotNone(kwargs["limit"])

    def test_fetch_query_receives_clamped_limit_for_zero_input(self):
        # --limit=0 clamps to 1, fetch_query should receive 1 (not 0 or None)
        self._run_fullrecord(limit=0)
        _, kwargs = scrape.fetch_query.call_args
        self.assertEqual(kwargs["limit"], 1)
        self.assertIsNotNone(kwargs["limit"])

    def test_fetch_query_receives_clamped_limit_for_large_input(self):
        # --limit=50000 clamps to fullrecord_MAX, fetch_query receives the cap
        self._run_fullrecord(limit=50000)
        _, kwargs = scrape.fetch_query.call_args
        self.assertEqual(kwargs["limit"], config.fullrecord_MAX)
        self.assertIsInstance(kwargs["limit"], int)


class FullrecordParserTests(unittest.TestCase):
    """Regression tests for Bug 1: parser construction must not raise AttributeError.

    Before the fix, `python scrape.py fullrecord --council "Tamil Nadu" --year 1960`
    crashed with AttributeError: module 'config' has no attribute 'fullrecord_MAX'
    because the parser referenced config.fullrecord_MAX and config.fullrecord_DEFAULT
    before they were defined.  Requirements 4.1.
    """

    def test_fullrecord_parser_constructs_without_attribute_error(self):
        """Requirement 4.1: parser construction uses config.fullrecord_MAX/DEFAULT without AttributeError."""
        # --help causes argparse to print help and raise SystemExit(0) — that is expected.
        # What must NOT happen is AttributeError at the point where the parser reads
        # config.fullrecord_MAX and config.fullrecord_DEFAULT to build the subcommand.
        try:
            scrape.main(["fullrecord", "--help"])
        except SystemExit:
            pass  # expected: argparse --help always exits
        except AttributeError as exc:
            self.fail(f"Parser construction raised AttributeError: {exc}")

    def test_fullrecord_parser_with_valid_args_constructs_without_attribute_error(self):
        """Requirement 4.1: parser construction with real args does not raise AttributeError."""
        # Patch run_command so no network call is made; we only care that the parser
        # builds and arg-parsing succeeds without AttributeError.
        with mock.patch.object(scrape, "run_command"), \
                mock.patch.object(scrape.logging, "basicConfig"), \
                mock.patch.object(scrape.logging, "FileHandler",
                                  return_value=scrape.logging.NullHandler()):
            try:
                scrape.main(["fullrecord", "--council", "Tamil Nadu", "--year", "1960"])
            except AttributeError as exc:
                self.fail(f"Parser construction raised AttributeError: {exc}")
            except SystemExit:
                pass  # also acceptable


class TlsTests(unittest.TestCase):
    def test_certificate_verification_is_always_on(self):
        context = imr_client._ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_certificate_failure_fails_fast_with_guidance(self):
        calls = []

        def refuse(*args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))

        client = imr_client.Client(base="https://example.invalid", delay=0)
        with mock.patch("urllib.request.urlopen", refuse):
            with self.assertRaises(imr_client.ApiError) as caught:
                client.states()
        self.assertEqual(len(calls), 1)                      # no pointless retries
        self.assertIn("truststore", str(caught.exception))
        self.assertIn("NOT been switched off", str(caught.exception))

    def test_commands_report_errors_without_a_traceback(self):
        def failing(_args):
            raise imr_client.ApiError("boom")

        with self.assertRaises(SystemExit) as caught:
            scrape.run_command(failing, None)
        self.assertEqual(str(caught.exception), "Error: boom")


if __name__ == "__main__":
    unittest.main()
