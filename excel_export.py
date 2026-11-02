"""Streaming XLSX export for the collector, using only the Python standard library.

This is application code: exports run in the user's existing Python environment.
Rows are spooled to temporary worksheet XML and zipped only after a successful read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import config
from year_filters import registration_year, validate_year_range

log = logging.getLogger("imr")

COLUMNS = (
    ("year_of_info", "Info YR"),
    ("registration_no", "Reg No"),
    ("state_medical_council", "Coucil"),
    ("name", "Name"),
    ("registration_date", "Date"),
    ("uprn_no", "UPRN No"),
    ("qualification", "Qualification"),
    ("qualification_year", "Yr of Qualification"),
    ("university", "University"),
    ("additional_qualifications", "Additional"),
    ("_council_code", "Coucil ID"),
    ("_council", "Coucil"),
    ("_year", "Year"),
    ("_scraped_at", "Scrapped @"),
)
WIDTHS = (10, 18, 42, 32, 15, 20, 25, 22, 45, 60, 12, 42, 10, 28)
MAX_DATA_ROWS = 1_048_575  # Excel row limit less the header row.
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@dataclass
class ExportStats:
    unique_rows: int = 0
    matching_rows: int = 0
    outside_registration_range: int = 0
    unknown_registration_dates: int = 0
    unreadable_lines: int = 0


def unique_records(src, stats=None):
    """Read fixed-schema records; ignore only collection timestamps for deduplication."""
    seen = set()
    with Path(src).open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
            except (ValueError, json.JSONDecodeError):
                if stats is not None:
                    stats.unreadable_lines += 1
                log.warning("Skipping unreadable record at line %d", number)
                continue
            record = {key: raw.get(key, "") for key, _ in COLUMNS}
            identity = json.dumps({key: value for key, value in record.items() if key != "_scraped_at"},
                                  sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(identity.encode("utf-8")).digest()
            if digest not in seen:
                seen.add(digest)
                yield record


def selected_records(src, *, registration_year_from=None, registration_year_to=None, stats=None):
    """Filter professional export rows by registration date, without changing source data."""
    validate_year_range(registration_year_from, registration_year_to)
    stats = stats if stats is not None else ExportStats()
    filtered = registration_year_from is not None or registration_year_to is not None
    for record in unique_records(src, stats=stats):
        stats.unique_rows += 1
        year = registration_year(record.get("registration_date"))
        if year is None:
            stats.unknown_registration_dates += 1
            if filtered:
                continue
        elif ((registration_year_from is not None and year < registration_year_from)
              or (registration_year_to is not None and year > registration_year_to)):
            stats.outside_registration_range += 1
            continue
        stats.matching_rows += 1
        yield record


def flat_values(record):
    values = []
    for key, _ in COLUMNS:
        value = record.get(key)
        if key == "additional_qualifications" and isinstance(value, list):
            parts = []
            for qualification in value:
                if not isinstance(qualification, dict):
                    continue
                bits = [str(qualification.get(field) or "").strip() for field in config.ALLOWED_ADDITIONAL]
                parts.append(" | ".join(bit for bit in bits if bit and bit != "-"))
            value = "; ".join(part for part in parts if part)
        values.append("" if value is None else str(value))
    return values


def council_group(record):
    code = str(record.get("_council_code") or "").strip()
    name = str(record.get("_council") or record.get("state_medical_council") or "").strip()
    group = ("code", code.casefold()) if code else ("name", name.casefold())
    if "tamil nadu" in name.casefold() or code.upper() == "TAM":
        label = "TN"
    elif "karnataka" in name.casefold() or code.upper() in ("KAR", "KAN"):
        label = "KAN"
    else:
        label = code or name or "Unknown"
    return group, label


def sheet_name(label, used):
    base = re.sub(r"[\[\]:*?/\\\x00-\x1f]", "_", label).strip().strip("'") or "Unknown"
    if base.casefold() == "history":
        base = "History_"
    candidate, suffix = base[:31], 1
    while candidate.casefold() in used:
        suffix += 1
        ending = f"_{suffix}"
        candidate = base[:31 - len(ending)] + ending
    used.add(candidate.casefold())
    return candidate


def excel_text(value):
    # Escape literal OOXML control-code notation before encoding XML-invalid controls.
    value = re.sub(r"_x[0-9a-fA-F]{4}_", lambda match: "_x005F_" + match.group()[1:], value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]",
                   lambda match: f"_x{ord(match.group()):04X}_", value)
    return escape(value)


def cell(column, row, value, header=False):
    ref = f"{chr(65 + column)}{row}"
    if len(value.encode("utf-16-le")) // 2 > 32767:
        raise ValueError(f"A value in column {COLUMNS[column][1]!r} exceeds Excel's cell text limit")
    style = 1 if header else 0
    if not header and value:
        if column in (0, 7, 12) and value.isascii() and value.isdecimal():
            return f'<c r="{ref}"><v>{int(value)}</v></c>'
        if column in (4, 13):
            try:
                if column == 13:
                    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if date.tzinfo:
                        date = date.astimezone(timezone.utc).replace(tzinfo=None)
                else:
                    date = None
                    for pattern in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
                        try:
                            date = datetime.strptime(value, pattern)
                            break
                        except ValueError:
                            pass
                    if date is None:
                        raise ValueError("unrecognized date")
                if date < datetime(1900, 1, 1):
                    raise ValueError("date outside supported Excel range")
                serial = (date - datetime(1899, 12, 31)).total_seconds() / 86400
                if date >= datetime(1900, 3, 1):
                    serial += 1  # Excel's historical fictitious leap day.
                return f'<c r="{ref}" s="{2 if column == 4 else 3}"><v>{serial:.10f}</v></c>'
            except ValueError:
                pass  # Preserve unrecognized/partial source dates verbatim.
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{excel_text(value)}</t></is></c>'


STYLES = f'''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="{NS}">
<numFmts count="2"><numFmt numFmtId="164" formatCode="dd-mm-yyyy"/><numFmt numFmtId="165" formatCode='yyyy-mm-dd hh:mm:ss "UTC"'/></numFmts>
<fonts count="2"><font><sz val="10"/><name val="Arial"/></font><font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Arial"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF244062"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def export_xlsx(src, dst, *, registration_year_from=None, registration_year_to=None, stats=None):
    """Return {sheet_name: data_rows}; atomically replace dst after a successful export."""
    src, dst = Path(src), Path(dst)
    validate_year_range(registration_year_from, registration_year_to)
    if src.resolve() == dst.resolve():
        raise ValueError("Export destination must differ from the source JSONL")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="imr-export-", dir=dst.parent) as temporary:
        temporary = Path(temporary)
        sheets, groups, used = [], {}, set()
        with ExitStack() as stack:
            def add_sheet(label):
                name = sheet_name(label, used)
                path = temporary / f"sheet{len(sheets) + 1}.xml"
                handle = stack.enter_context(path.open("w", encoding="utf-8", newline=""))
                handle.write(f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{NS}">'
                             '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
                             '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                             '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
                             '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/><cols>')
                for number, width in enumerate(WIDTHS, 1):
                    handle.write(f'<col min="{number}" max="{number}" width="{width}" customWidth="1"/>')
                handle.write('</cols><sheetData><row r="1" ht="30" customHeight="1">')
                handle.write("".join(cell(index, 1, title, header=True) for index, (_, title) in enumerate(COLUMNS)))
                handle.write('</row>')
                sheet = {"name": name, "path": path, "handle": handle, "rows": 0}
                sheets.append(sheet)
                return sheet

            for record in selected_records(src, registration_year_from=registration_year_from,
                                           registration_year_to=registration_year_to, stats=stats):
                group, label = council_group(record)
                sheet = groups.get(group)
                if sheet is None or sheet["rows"] >= MAX_DATA_ROWS:
                    sheet = groups[group] = add_sheet(label)
                sheet["rows"] += 1
                row = sheet["rows"] + 1
                sheet["handle"].write(f'<row r="{row}">' + ''.join(
                    cell(index, row, value) for index, value in enumerate(flat_values(record))) + '</row>')
            if not sheets:
                add_sheet("No data")
            for sheet in sheets:
                sheet["handle"].write(f'</sheetData><autoFilter ref="A1:N{sheet["rows"] + 1}"/></worksheet>')

        staged = temporary / "workbook.xlsx"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                             f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            workbook = f'<workbook xmlns="{NS}" xmlns:r="{REL_NS}"><sheets>'
            relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            types = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            types += '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            for index, sheet in enumerate(sheets, 1):
                workbook += f'<sheet name={quoteattr(sheet["name"])} sheetId="{index}" r:id="rId{index}"/>'
                relationships += f'<Relationship Id="rId{index}" Type="{REL_NS}/worksheet" Target="worksheets/sheet{index}.xml"/>'
                types += f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                archive.write(sheet["path"], f"xl/worksheets/sheet{index}.xml")
            relationships += f'<Relationship Id="rIdStyles" Type="{REL_NS}/styles" Target="styles.xml"/></Relationships>'
            archive.writestr("xl/workbook.xml", workbook + '</sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", relationships)
            archive.writestr("xl/styles.xml", STYLES)
            archive.writestr("[Content_Types].xml", types + '</Types>')
        staged.replace(dst)
    return {sheet["name"]: sheet["rows"] for sheet in sheets}
