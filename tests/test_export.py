"""Offline checks of the native XLSX/CSV exports using synthetic register entries."""
import csv
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from argparse import Namespace
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

import config
import excel_export as excel
import scrape

NS = {"s": excel.NS}


def record(code="TAM", council="Tamil Nadu Medical Council", **changes):
    return {"year_of_info": "2025", "registration_no": "00123", "state_medical_council": council,
            "name": "TEST PERSON", "registration_date": "01-02-2025", "uprn_no": "0000042",
            "qualification": "MBBS", "qualification_year": "2020", "university": "TEST UNIVERSITY",
            "additional_qualifications": [{"qualification": "MD", "year": "2023", "university": "TEST UNI"}],
            "_council_code": code, "_council": council, "_year": "2025",
            "_scraped_at": "2026-10-28T10:00:00+00:00", **changes}


def text(cell):
    return ''.join(cell.itertext())


class CollectControlCharacterTests(unittest.TestCase):
    def test_both_collect_writers_save_and_reopen_control_characters(self):
        import openpyxl

        dirty = ''.join(chr(n) for n in (*range(9), 11, 12, *range(14, 32)))
        original = {"qualification": dirty + "M.B.B.S.",
                    "notes": "தமிழ்\tline\nbreak\r", "count": 42}
        with tempfile.TemporaryDirectory() as directory:
            for multi in (False, True):
                with self.subTest(multi=multi):
                    dst = Path(directory) / f"export-{multi}.xlsx"
                    fields = list(original)
                    if multi:
                        scrape._write_xlsx_multi_sheet({"Council": [original]}, fields, dst)
                    else:
                        scrape._write_xlsx([original], fields, dst, "Council")
                    book = openpyxl.load_workbook(dst)
                    try:
                        self.assertEqual(book.active["A2"].value, "M.B.B.S.")
                        # XML readers normalize literal carriage returns to newlines.
                        self.assertEqual(book.active["B2"].value, original["notes"].replace("\r", "\n"))
                        self.assertEqual(scrape._xlsx_clean(original["notes"]), original["notes"])
                        self.assertEqual(book.active["C2"].value, 42)
                    finally:
                        book.close()
        self.assertEqual(original["qualification"], dirty + "M.B.B.S.")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "imr_records.jsonl"
        self.dst = self.root / "out.xlsx"

    def seed(self, rows):
        self.src.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding="utf-8")

    def test_council_sheets_headers_and_distinct_qualifications(self):
        rows = [record(), record(_scraped_at="2026-10-29T00:00:00Z"),
                record(qualification="MD"), record("KAN", "Karnataka Medical Council")]
        self.seed(rows)
        result = excel.export_xlsx(self.src, self.dst)
        self.assertEqual(result, {"TN": 2, "KAN": 1})
        with ZipFile(self.dst) as archive:
            self.assertIsNone(archive.testzip())
            for path in archive.namelist():
                if path.endswith(".xml") or path.endswith(".rels"):
                    ET.fromstring(archive.read(path))
            book = ET.fromstring(archive.read("xl/workbook.xml"))
            self.assertEqual([sheet.get("name") for sheet in book.find("s:sheets", NS)], ["TN", "KAN"])
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            xmlrows = sheet.findall("s:sheetData/s:row", NS)
            self.assertEqual([text(cell) for cell in xmlrows[0]], [title for _, title in excel.COLUMNS])
            self.assertEqual(len(xmlrows), 3)
            self.assertEqual(text(xmlrows[1][1]), "00123")
            self.assertEqual(xmlrows[1][1].get("t"), "inlineStr")
            self.assertEqual(text(xmlrows[1][5]), "0000042")
            self.assertEqual(text(xmlrows[1][9]), "MD | 2023 | TEST UNI")
            self.assertEqual(xmlrows[1][0].find("s:v", NS).text, "2025")
            self.assertEqual(xmlrows[1][4].get("s"), "2")
            self.assertEqual(xmlrows[1][13].get("s"), "3")
            self.assertEqual(sheet.find("s:sheetViews/s:sheetView/s:pane", NS).get("state"), "frozen")
            self.assertEqual(sheet.find("s:autoFilter", NS).get("ref"), "A1:N3")

    def test_source_text_is_not_interpreted_as_formula(self):
        self.seed([record(name='=HYPERLINK("https://example.invalid")', university="தமிழ் & <test>")])
        excel.export_xlsx(self.src, self.dst)
        with ZipFile(self.dst) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            self.assertEqual(sheet.findall(".//s:f", NS), [])
            cells = sheet.findall("s:sheetData/s:row", NS)[1]
            self.assertEqual(text(cells[3]), '=HYPERLINK("https://example.invalid")')
            self.assertEqual(text(cells[8]), "தமிழ் & <test>")

    def test_council_sheet_names_are_valid_and_unique(self):
        self.seed([record("bad/name", "A"), record("bad:name", "B"), record("HISTORY", "C")])
        result = excel.export_xlsx(self.src, self.dst)
        self.assertEqual(list(result), ["bad_name", "bad_name_2", "History_"])

    def test_sheet_row_limit_rolls_over_without_losing_records(self):
        self.seed([record(registration_no=str(n)) for n in range(5)])
        with mock.patch.object(excel, "MAX_DATA_ROWS", 2):
            self.assertEqual(excel.export_xlsx(self.src, self.dst), {"TN": 2, "TN_2": 2, "TN_3": 1})

    def test_empty_input_produces_header_only_workbook(self):
        self.seed([])
        self.assertEqual(excel.export_xlsx(self.src, self.dst), {"No data": 0})

    def test_failed_export_preserves_existing_output(self):
        self.seed([record(name="x" * 32768)])
        self.dst.write_bytes(b"previous output")
        with self.assertRaises(ValueError):
            excel.export_xlsx(self.src, self.dst)
        self.assertEqual(self.dst.read_bytes(), b"previous output")

    def test_csv_uses_same_headers_and_deduplication(self):
        self.seed([record(), record(), record(qualification="MD")])
        dst = self.root / "out.csv"
        self.assertEqual(scrape.export_csv(self.src, dst), 2)
        with dst.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], [title for _, title in excel.COLUMNS])
        self.assertEqual(rows[1][1], "00123")

    def test_default_export_is_workbook_and_csv_still_supported(self):
        self.seed([record()])
        with mock.patch.object(config, "DATA_DIR", self.root):
            scrape.run_export(Namespace(out=None, format=None))
            self.assertTrue((self.root / "imr_records.xlsx").is_file())
            scrape.run_export(Namespace(out=str(self.root / "out.csv"), format=None))
            self.assertTrue((self.root / "out.csv").is_file())


if __name__ == "__main__":
    unittest.main()
