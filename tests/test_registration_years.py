"""V1 registration-year views: professional exports only, without checkpoint changes."""
import csv
import io
import json
import logging
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

import config
import excel_export as excel
import scrape
from year_filters import registration_year, validate_year_range

ROOT = Path(__file__).resolve().parents[1]


def record(number, date, *, info="1960", qualification="MBBS", **changes):
    return {"year_of_info": info, "registration_no": number, "state_medical_council": "Tamil Nadu Medical Council",
            "name": "SYNTHETIC TEST DOCTOR", "registration_date": date, "uprn_no": "0000123",
            "qualification": qualification, "qualification_year": "1950", "university": "TEST UNIVERSITY",
            "additional_qualifications": [], "_council_code": "TAM", "_council": "Tamil Nadu Medical Council",
            "_year": info, "_scraped_at": "2026-10-31T00:00:00Z", **changes}


class DateParsingTests(unittest.TestCase):
    def test_supported_source_dates(self):
        for value in ("01-02-1960", "01/02/1960", "01.02.1960", "1960/02/01", "1960-02-01",
                      "1960-02-01T12:00:00Z", "01-02-1960 00:00:00", " 1-2-1960 "):
            with self.subTest(value=value):
                self.assertEqual(registration_year(value), 1960)

    def test_unknown_or_incomplete_dates_are_not_guessed(self):
        for value in (None, "", "N/A", "-", "1960", "01-01-60", "31-02-1960", "29-02-1959",
                      "February 1960", "not-a-date", 1960, {"year": 1960}):
            with self.subTest(value=value):
                self.assertIsNone(registration_year(value))

    def test_timestamp_preserves_written_calendar_year(self):
        self.assertEqual(registration_year("1960-12-31T23:59:00-08:00"), 1960)

    def test_invalid_year_bounds_are_rejected(self):
        for first, last in ((1961, 1960), (0, 1960), (1960, 10000), (True, None), ("1960", None)):
            with self.subTest(first=first, last=last):
                with self.assertRaises(ValueError):
                    validate_year_range(first, last)


class RegistrationExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.src = self.data / "imr_records.jsonl"
        self.dst = self.data / "filtered.xlsx"
        patch = mock.patch.object(config, "DATA_DIR", self.data)
        patch.start()
        self.addCleanup(patch.stop)

    def seed(self, records):
        self.src.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding="utf-8")

    def export(self, **changes):
        args = Namespace(**{**dict(out=str(self.dst), format=None,
                                   registration_year_from=1960, registration_year_to=1960), **changes})
        captured = io.StringIO()
        with redirect_stdout(captured):
            scrape.run_export(args)
        return captured.getvalue()

    def test_information_year_is_not_used_as_registration_year(self):
        self.seed([record("001", "01-01-1915"), record("002", "01-01-1959"), record("003", "01-01-1960")])
        stats = excel.ExportStats()
        selected = list(excel.selected_records(self.src, registration_year_from=1960,
                                               registration_year_to=1960, stats=stats))
        self.assertEqual([row["registration_no"] for row in selected], ["003"])
        self.assertEqual(stats.unique_rows, 3)
        self.assertEqual(stats.outside_registration_range, 2)

    def test_both_boundaries_are_inclusive(self):
        self.seed([record("001", "31-12-1959"), record("002", "01-01-1960"),
                   record("003", "31-12-1961"), record("004", "01-01-1962")])
        selected = list(excel.selected_records(self.src, registration_year_from=1960, registration_year_to=1961))
        self.assertEqual([row["registration_no"] for row in selected], ["002", "003"])

    def test_one_sided_ranges(self):
        self.seed([record("001", "01-01-1959"), record("002", "01-01-1960"), record("003", "01-01-1961")])
        self.assertEqual(len(list(excel.selected_records(self.src, registration_year_from=1960))), 2)
        self.assertEqual(len(list(excel.selected_records(self.src, registration_year_to=1960))), 2)

    def test_unknown_dates_are_reported_and_source_is_preserved(self):
        self.seed([record("001", None), record("002", "1960"), record("003", "01-01-1960")])
        original = self.src.read_bytes()
        output = self.export()
        self.assertIn("Unknown/missing registration dates: 2", output)
        self.assertIn("excluded from the year-filtered export; preserved in source", output)
        self.assertIn("exported: 1", output)
        self.assertEqual(self.src.read_bytes(), original)

    def test_unfiltered_export_keeps_unknown_dates(self):
        self.seed([record("001", "not-a-date"), record("002", None), record("003", "01-01-1960")])
        stats = excel.ExportStats()
        self.assertEqual(excel.export_xlsx(self.src, self.dst, stats=stats), {"TN": 3})
        self.assertEqual(stats.unknown_registration_dates, 2)
        self.assertEqual(stats.matching_rows, 3)

    def test_headers_tabs_identifiers_and_information_year_are_preserved(self):
        self.seed([record("00123", "01-01-1960", info="2025"), record("00999", "01-01-1959", info="1960")])
        self.export()
        with ZipFile(self.dst) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            ns = {"s": excel.NS}
            rows = sheet.findall("s:sheetData/s:row", ns)
            self.assertEqual([''.join(cell.itertext()) for cell in rows[0]], [title for _, title in excel.COLUMNS])
            self.assertEqual(len(rows), 2)
            self.assertEqual(''.join(rows[1][0].itertext()), "2025")
            self.assertEqual(''.join(rows[1][1].itertext()), "00123")
            self.assertEqual(rows[1][1].get("t"), "inlineStr")
            self.assertEqual(''.join(rows[1][12].itertext()), "2025")

    def test_csv_and_workbook_have_same_selection(self):
        self.seed([record("001", "01-01-1959"), record("002", "01-01-1960"), record("003", "unknown")])
        self.assertEqual(excel.export_xlsx(self.src, self.dst, registration_year_from=1960,
                                          registration_year_to=1960), {"TN": 1})
        csv_path = self.data / "filtered.csv"
        self.assertEqual(scrape.export_csv(self.src, csv_path, registration_year_from=1960,
                                           registration_year_to=1960), 1)
        with csv_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[1][1], "002")

    def test_distinct_qualifications_survive_filtered_deduplication(self):
        self.seed([record("001", "01-01-1960"), record("001", "01-01-1960"),
                   record("001", "01-01-1960", qualification="MD")])
        self.assertEqual(excel.export_xlsx(self.src, self.dst, registration_year_from=1960), {"TN": 2})

    def test_incomplete_source_is_not_cleared_by_small_filtered_output(self):
        self.seed([record("001", "01-01-1959")])
        state = scrape.State(self.data / "state.json")
        state.mark("TAM", "1960", 1, 11517, True)
        original_state = (self.data / "state.json").read_bytes()
        with self.assertLogs("imr", level="WARNING") as warning:
            output = self.export()
        self.assertIn("exported: 0", output)
        self.assertIn("1 incomplete/capped source batches", output)
        self.assertIn("not a complete registration-year total", output)
        self.assertIn("do not clear", '\n'.join(warning.output))
        self.assertEqual((self.data / "state.json").read_bytes(), original_state)

    def test_filtered_default_filename_preserves_unfiltered_workbook(self):
        self.seed([record("001", "01-01-1960")])
        existing = self.data / "imr_records.xlsx"
        existing.write_bytes(b"existing unfiltered export")
        self.export(out=None)
        self.assertTrue((self.data / "imr_records_registration_1960-1960.xlsx").exists())
        self.assertEqual(existing.read_bytes(), b"existing unfiltered export")

    def test_unknown_source_coverage_is_not_claimed_complete(self):
        self.seed([record("001", "01-01-1960")])
        output = self.export()
        self.assertIn("Source coverage: unknown", output)
        self.assertIn("saved records only", output)

    def test_export_never_emits_other_source_fields(self):
        self.seed([record("001", "01-01-1960", mobile="TEST_PRIVATE_PHONE", permanent_address="TEST_PRIVATE_ADDRESS")])
        self.export()
        with ZipFile(self.dst) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml")
        self.assertNotIn(b"TEST_PRIVATE", sheet)

    def test_unreadable_lines_are_counted_without_rewriting_source(self):
        self.seed([record("001", "01-01-1960")])
        with self.src.open("a") as handle:
            handle.write('{"incomplete":')
        original = self.src.read_bytes()
        stats = excel.ExportStats()
        with self.assertLogs("imr", level="WARNING"):
            excel.export_xlsx(self.src, self.dst, registration_year_from=1960, stats=stats)
        self.assertEqual(stats.unreadable_lines, 1)
        self.assertEqual(self.src.read_bytes(), original)

    def test_information_year_aliases_keep_existing_destinations(self):
        with mock.patch.object(scrape, "run_command") as command, \
                mock.patch.object(logging, "basicConfig"), \
                mock.patch.object(logging, "FileHandler", return_value=logging.NullHandler()):
            for subcommand in ("scrape", "batch"):
                for first, last in (("--year-from", "--year-to"), ("--info-year-from", "--info-year-to")):
                    scrape.main([subcommand, first, "1950", last, "1979"])
                    args = command.call_args.args[1]
                    self.assertEqual((args.year_from, args.year_to), (1950, 1979))

    def test_invalid_registration_ranges_fail_before_export(self):
        for arguments in (["export", "--registration-year-from", "1961", "--registration-year-to", "1960"],
                          ["export", "--registration-year-from", "0"],
                          ["export", "--registration-year-to", "10000"],
                          ["scrape", "--registration-year-from", "1960"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(ROOT / "scrape.py"), *arguments],
                                        cwd=self.data, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
