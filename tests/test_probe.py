"""Tests for probe.py against a mock whose filter behaviour can be switched.

The data is fake. The tests also check that no name from the data is ever printed.
Run from the repo root:  python -m unittest discover -s tests -v
"""
import json
import math
import os
import random
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import imr_client  # noqa: E402
import probe  # noqa: E402

FIRST = ["Arun", "Arul", "Karthik", "Meena", "Ravi", "Aparna", "Siva", "Kavitha", "Prakash", "Lakshmi", "Divya", "Naveen"]
LAST = ["Kumar", "Raja", "Devi", "Shankar", "Nair", "Rani", "Narayanan", "Prasad", "Murthy", "Iyer", "Anand"]
DATA = [{"year_of_info": "2025", "registration_no": str(200000 + i),
         "state_medical_council": "Tamil Nadu Medical Council", "name": f"{f} {l}"}
        for i, (f, l) in enumerate((f, l) for f in FIRST for l in LAST)]


def matches(value, term, mode):
    value, term = value.lower(), term.lower()
    if not term:
        return True
    if mode == "prefix":
        return value.startswith(term)
    if mode == "word":
        return any(w.startswith(term) for w in value.split())
    if mode == "exact":
        return value == term
    return term in value  # "contains"


class Handler(BaseHTTPRequestHandler):
    cfg = {"name": "prefix", "reg": "prefix", "unstable": False, "min_name": 0}

    def log_message(self, *a):
        pass

    def _send(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        if url.path.endswith("/states"):
            return self._send({"states": [{"code": "TAM", "name": "Tamil Nadu Medical Council"}]})
        if url.path.endswith("/years"):
            return self._send({"years": ["2025"]})
        name, reg = q.get("name", ""), q.get("reg_no", "")
        if name and len(name) < self.cfg["min_name"]:
            return self._send({"message": "too short"}, 422)
        rows = [r for r in DATA if matches(r["name"], name, self.cfg["name"])
                and matches(r["registration_no"], reg, self.cfg["reg"])]
        if self.cfg["unstable"]:
            random.shuffle(rows)
        page, per = int(q["page"]), min(int(q["per_page"]), 100)
        chunk = rows[(page - 1) * per: page * per]
        self._send({"success": bool(chunk), "data": chunk,
                    "pagination": {"total": len(rows), "per_page": per, "current_page": page,
                                   "total_pages": math.ceil(len(rows) / per) if rows else 0}})


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/indian-medical-register"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_probe(self, **cfg):
        Handler.cfg = {"name": "prefix", "reg": "prefix", "unstable": False, "min_name": 0, **cfg}
        lines = []
        with tempfile.TemporaryDirectory() as tmp:
            res = probe.run_probe(imr_client.Client(base=self.base, delay=0), "TAM",
                                  "Tamil Nadu Medical Council", 2025, out=lines.append, data_dir=Path(tmp))
        return res, "\n".join(lines)

    def test_prefix_filters_are_detected_and_registration_split_is_recommended(self):
        res, text = self.run_probe()
        self.assertEqual(res["name_mode"], "starts-with")
        self.assertEqual(res["reg_mode"], "starts-with")
        self.assertTrue(res["filters_combine"])
        self.assertTrue(res["stable_order"])
        self.assertEqual(res["paging_overlap"], 0)
        self.assertTrue(res["reg_exact_ok"])
        self.assertIn("registration-number prefix", res["recommendation"])
        self.assertLessEqual(res["requests"], 9)

    def test_name_prefix_only_recommends_a_name_split(self):
        res, _ = self.run_probe(reg="exact")
        self.assertEqual(res["reg_mode"], "exact only")
        self.assertEqual(res["name_mode"], "starts-with")
        self.assertIn("name prefix", res["recommendation"])

    def test_substring_filters_are_detected_as_overlapping(self):
        res, _ = self.run_probe(name="contains", reg="contains")
        self.assertEqual(res["name_mode"], "contains")
        self.assertEqual(res["reg_mode"], "contains")
        self.assertIn("overlap", res["recommendation"])

    def test_word_start_name_filter_is_detected(self):
        res, _ = self.run_probe(name="word", reg="exact")
        self.assertEqual(res["name_mode"], "word-start")

    def test_minimum_name_length_is_handled(self):
        res, _ = self.run_probe(min_name=3)
        self.assertEqual(res["name_mode"], "starts-with")

    def test_unstable_paging_is_flagged(self):
        res, text = self.run_probe(unstable=True)
        self.assertFalse(res["stable_order"])
        self.assertIn("unstable", res["recommendation"])

    def test_no_names_are_printed(self):
        _, text = self.run_probe()
        for word in FIRST + LAST:
            self.assertNotIn(word, text)
        for record in DATA[:20]:
            self.assertNotIn(record["registration_no"], text)

    def test_local_duplicates_are_reported(self):
        Handler.cfg = {"name": "prefix", "reg": "prefix", "unstable": False, "min_name": 0}
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"_council_code": "TAM", "_year": "2025", "registration_no": r} for r in ("1", "2", "2", "3")]
            Path(tmp, "imr_records.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            lines = []
            res = probe.run_probe(imr_client.Client(base=self.base, delay=0), "TAM",
                                  "Tamil Nadu Medical Council", 2025, out=lines.append, data_dir=Path(tmp))
        self.assertEqual(res["local"], (4, 3))
        self.assertIn("duplicates", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
