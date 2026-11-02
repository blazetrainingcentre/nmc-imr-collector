"""NMC Indian Medical Register collector (JSON route; no browser needed).

Commands
    python scrape.py list                     councils and years offered by the site
    python scrape.py scrape [--council NAME] [--year-from Y] [--year-to Y]
                            [--delay 1.5] [--max-queries N] [--dry-run]
    python scrape.py collect --council NAME --year Y [--delay 1.5]
                            Full records (all fields, no limit) saved to data/collect/collect.jsonl.
                            Runs automatic recovery if the site caps the result window.
    python scrape.py sample --council NAME --year Y [--limit 100]
                            TEST ONLY: full records (all fields), capped at 500
    python scrape.py status                   progress and any capped searches
    python scrape.py export [--out data/imr_records.csv]
    python scrape.py export-xlsx [--src data/local_records.jsonl] [--out data/local_records.xlsx]
                            Export JSONL to Excel; one sheet per council, deduped
    python scrape.py export-collect [--out-dir data/collect/]
                            Export data/collect/collect.jsonl into one xlsx per council
    python scrape.py export-collect-combined [--out data/collect/collect.xlsx]
                            Export data/collect/collect.jsonl into a single xlsx, one sheet per council

Notes
  * `scrape` keeps only config.ALLOWED_FIELDS; date of birth, father/husband name and
    permanent address are dropped before anything is written.
  * `collect` keeps every field the API returns with NO record limit, uses the same
    capped-recovery logic as `scrape`, and writes to data/collect/collect.jsonl.
    Keep that file local and do not commit or share it.
  * `sample` keeps every field the API returns, but never more than config.SAMPLE_MAX
    records, and writes to data/sample_full.jsonl. Keep that file local and delete it
    after testing.
  * The site serves at most 10,000 results per search. Automatic recovery uses overlapping
    registration filters and local deduplication; incomplete batches stay flagged "capped".
  * Sequential requests with jittered pauses. HTTP 403/429 or a non-JSON reply stops the run;
    nothing here tries to get around it.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import string
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import config
from imr_client import ApiError, Blocked, Client

log = logging.getLogger("imr")
RECOVERY_IDENTITY_VERSION = 2


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def real_councils(councils):
    """Drop the site's catch-all 'All' entry; each council is queried on its own."""
    return [(code, name) for code, name in councils
            if str(code).strip().lower() != "all" and name.strip().lower() != "all"]


def build_plan(councils, years, only=None, year_from=None, year_to=None):
    """Build a year-major plan: every council for a year, then the next year.

    Keeping the year as the outer loop makes long unattended runs easier to
    monitor and lets an operator stop after a completed year.  State keys are
    still council+year, so existing progress files remain compatible.
    """
    plan = []
    selected_councils = [
        (code, name) for code, name in real_councils(councils)
        if not only or any(o.lower() in name.lower() for o in only)
    ]
    for year in years:
        try:
            y = int(year)
        except (TypeError, ValueError):
            y = None
        if y is not None:
            if year_from and y < year_from:
                continue
            if year_to and y > year_to:
                continue
        for code, name in selected_councils:
            plan.append((code, name, year))
    return plan


def keep_allowed(item: dict) -> dict:
    """Allow-list a raw API record. Everything not listed in config is dropped."""
    out = {}
    for key in config.ALLOWED_FIELDS:
        value = item.get(key)
        if key == "additional_qualifications":
            extras = value if isinstance(value, list) else []
            value = [{k: str(q.get(k, "") or "") for k in config.ALLOWED_ADDITIONAL}
                     for q in extras if isinstance(q, dict)]
        elif value is None:
            value = ""
        out[key] = value
    return out


def to_record(item: dict, code: str, council: str, year: str) -> dict:
    return {**keep_allowed(item), "_council_code": code, "_council": council, "_year": year}


def to_record_full(item: dict, code: str, council: str, year: str) -> dict:
    """Preserve every API field, adding only collector metadata.

    `collect` is intentionally not an allow-listed export.  Copying the API
    object also keeps newly added fields without requiring a config change.
    """
    return {**item, "_council_code": code, "_council": council, "_year": year}


class State:
    """Remembers which (council, year) searches are finished."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.done: dict = {}
        self.recovery: dict = {}
        self.recovery_versions: dict = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.done = saved.get("done", {})
            self.recovery = saved.get("recovery", {})
            self.recovery_versions = saved.get("recovery_versions", {})

    @staticmethod
    def key(code: str, year: str) -> str:
        return f"{code}|{year}"

    def is_done(self, code: str, year: str) -> bool:
        return self.key(code, year) in self.done

    def mark(self, code: str, year: str, rows: int, total: int, capped: bool) -> None:
        self.done[self.key(code, year)] = {"rows": rows, "site_total": total,
                                           "capped": capped, "at": now_iso()}
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"done": self.done, "recovery": self.recovery,
                                   "recovery_versions": self.recovery_versions}, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def append_records(records, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = now_iso()
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps({**record, "_scraped_at": stamp}, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def flatten(record: dict) -> dict:
    out = dict(record)
    extras = out.get("additional_qualifications")
    if isinstance(extras, list):
        parts = []
        for q in extras:
            bits = [str(q.get(k, "")).strip() for k in config.ALLOWED_ADDITIONAL]
            bits = [b for b in bits if b and b != "-"]
            if bits:
                parts.append(" | ".join(bits))
        out["additional_qualifications"] = "; ".join(parts)
    return out


def export_csv(src: Path, dst: Path, *, registration_year_from=None,
               registration_year_to=None, stats=None) -> int:
    """Export the same fixed schema and selection as the XLSX exporter."""
    import excel_export

    selected = excel_export.selected_records(
        src, registration_year_from=registration_year_from,
        registration_year_to=registration_year_to, stats=stats)
    rows = [excel_export.flat_values(record) for record in selected]
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([title for _, title in excel_export.COLUMNS])
        writer.writerows(rows)
    return len(rows)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_query(client: Client, code: str, year: str, limit: int | None = None,
                reg_no: str = "", split_large: bool = False):
    """Walk the result pages of one (council, year) search.

    Returns (raw_items, site_total, capped). `capped` is True when the site holds more
    records than it will serve (page * per_page > MAX_WINDOW). `limit` stops early.
    """
    items, total, capped, page = [], 0, False, 1
    while True:
        response = client.search(code, year, page, config.PER_PAGE, reg_no=reg_no)
        data = response.get("data") or []
        if not (response.get("success") and data):
            reported = int((response.get("pagination") or {}).get("total") or 0)
            message = str(response.get("message") or "").lower()
            if page > 1 or reported or (message and "no records" not in message):
                raise ApiError("Search ended unexpectedly; leaving it unfinished")
            break  # the page's "No records found" case
        pagination = response.get("pagination") or {}
        total = int(pagination.get("total") or len(data))
        per_page = int(pagination.get("per_page") or config.PER_PAGE)
        total_pages = int(pagination.get("total_pages") or math.ceil(total / per_page))
        max_pages = max(1, config.MAX_WINDOW // per_page)
        items.extend(data)
        if split_large and total > config.MAX_WINDOW:
            return items, total, True  # use narrower filters instead of paging a capped fragment
        if limit and len(items) >= limit:
            return items[:limit], total, False
        if page >= total_pages:
            break
        if page >= max_pages:
            capped = True
            break
        page += 1
    return items, total, capped or len(items) < total


def recovery_identity(record: dict, *, full: bool = False) -> tuple:
    """Deduplicate exact allow-listed records, preserving qualification variations.

    One registration can have multiple source entries with different qualifications
    or universities. Council/year scope is supplied by the caller; scraping timestamps
    and private source fields never participate in this identity.
    """
    value = record if full else keep_allowed(record)
    return (json.dumps(value, sort_keys=True, ensure_ascii=False),)


def recover_capped(client, code, name, year, state, records_path, budget=200, max_depth=3,
                   record_fn=None, full_records=False):
    """Bounded overlapping contains searches. Only an equal unique count clears capped.

    Extensions are a search heuristic, not disjoint partitions or a proof of coverage.
    Missing/short/non-ASCII registration numbers may remain unreachable.

    record_fn: callable(item, code, name, year) -> dict  defaults to to_record (allow-listed).
               Pass to_record_full to keep every API field.

    GUARD: always pass record_fn=to_record_full when calling from run_collect; the default
    (to_record) silently strips DOB, father's name, address and other COLLECT_FIELDS before
    writing, which cannot be recovered without re-fetching from the API.
    """
    if record_fn is None:
        record_fn = to_record
    seen = set()
    if records_path.exists():
        with records_path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ApiError(f"Invalid local JSONL at line {line_no}; repair before recovery") from exc
                if str(record.get("_council_code")) == str(code) and str(record.get("_year")) == str(year):
                    seen.add(recovery_identity(record, full=full_records))

    _, total, _ = fetch_query(client, code, year, limit=1)
    key = state.key(code, year)
    previous = state.done.get(key, {})
    if previous.get("site_total", total) != total:
        raise ApiError("Site total changed since collection; use a fresh IMR_DATA_DIR for a new snapshot")
    if state.recovery_versions.get(key) != RECOVERY_IDENTITY_VERSION:
        if state.recovery.get(key):
            log.warning("Updating recovery deduplication for %s %s: preserving distinct qualifications "
                        "and rechecking old fragment searches. Saved records are retained.", name, year)
        # Older checkpoints may have discarded distinct source entries. Revisit those
        # fragments once using the new identity; never remove the saved records.
        state.recovery[key] = {}
        state.recovery_versions[key] = RECOVERY_IDENTITY_VERSION
        state.save()
    completed = state.recovery.setdefault(key, {})
    alphabet = string.digits + string.ascii_lowercase
    pending = deque(alphabet)
    searches = 0
    while pending and len(seen) < total:
        fragment = pending.popleft()
        if fragment == "0":
            # The live API treats the string "0" as empty and ignores this filter.
            # Expand it without requesting or trusting its unfiltered results.
            capped = True
        elif fragment in completed:
            capped = completed[fragment]
        else:
            if searches >= budget:
                break
            items, _, capped = fetch_query(client, code, year, reg_no=fragment, split_large=True)
            searches += 1
            mismatches = {
                "year": sum(str(item.get("year_of_info", "")) != str(year) for item in items),
                "council": sum(str(item.get("state_medical_council", "")).strip().casefold()
                               != name.strip().casefold() for item in items),
                "registration": sum(fragment not in str(item.get("registration_no") or "").casefold()
                                    for item in items),
            }
            if any(mismatches.values()):
                raise ApiError(f"Filtered response failed validation for fragment {fragment!r}: "
                               f"year mismatches={mismatches['year']}, "
                               f"council mismatches={mismatches['council']}, "
                               f"registration mismatches={mismatches['registration']}")
            fresh = []
            for item in items:
                ident = recovery_identity(item, full=full_records)
                if ident not in seen:
                    seen.add(ident)
                    fresh.append(record_fn(item, code, name, year))
            append_records(fresh, records_path)
            completed[fragment] = capped
            state.mark(code, year, len(seen), total, True)
            log.info("Recovery %s %s: %d/%d unique rows; %d filtered searches this run",
                     name, year, len(seen), total, searches)
        if capped and len(fragment) < max_depth:
            pending.extend(fragment + char for char in alphabet)

    # Compare totals again: the API is not a snapshot and can change during a run.
    _, final_total, _ = fetch_query(client, code, year, limit=1)
    incomplete = len(seen) != total or final_total != total
    state.mark(code, year, len(seen), total, incomplete)
    if incomplete:
        log.warning("INCOMPLETE %s %s: %d unique rows, expected %d (current total %d). "
                    "Recovery budget/depth or filter coverage may be insufficient. See status.",
                    name, year, len(seen), total, final_total)
    else:
        log.info("RECOVERED %s %s: %d unique rows match the site total", name, year, len(seen))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def run_list(args) -> None:
    client = Client(delay=args.delay)
    councils, years = real_councils(client.states()), client.years()
    print(f"{len(councils)} councils:")
    for code, name in councils:
        print(f"  {code:<6} {name}")
    if years:
        print(f"{len(years)} years, from {min(years)} to {max(years)}")


def run_batch(args) -> None:
    """Run a separate, explicitly selected council across the entire year range."""
    client = Client(delay=args.delay)
    councils = real_councils(client.states())
    if args.council:
        councils = [(code, name) for code, name in councils
                    if any(term.lower() in name.lower() for term in args.council)]
    years = []
    for value in client.years():
        try:
            year = int(value)
        except (TypeError, ValueError):
            continue
        if ((args.year_from is None or year >= args.year_from)
                and (args.year_to is None or year <= args.year_to)):
            years.append(str(year))
    years = sorted(set(years), key=int)
    if not councils or not years:
        raise ApiError("No councils or offered years match the requested range")
    for number, (code, name) in enumerate(councils, 1):
        log.info("COUNCIL [%d/%d] %s (%s): years %s-%s",
                 number, len(councils), name, code, years[0], years[-1])
        selected = argparse.Namespace(**{**vars(args), "council": [name]})
        run_scrape(selected, client=client, councils=[(code, name)], years=years,
                   stop_on_block=True)


def run_scrape(args, *, client=None, councils=None, years=None, stop_on_block=False) -> None:
    client = client if client is not None else Client(delay=args.delay)
    state = State(config.DATA_DIR / "state.json")
    records_path = config.DATA_DIR / "imr_records.jsonl"

    try:
        if councils is None:
            councils = client.states()
        if years is None:
            years = client.years()
    except (Blocked, ApiError) as exc:
        log.error("Could not load the council/year lists: %s", exc)
        return
    plan = build_plan(councils, years, args.council, args.year_from, args.year_to)
    recover = getattr(args, "recover_capped", True)
    todo = [p for p in plan if not state.is_done(p[0], p[2])
            or (recover and state.done[state.key(p[0], p[2])].get("capped"))]
    if args.max_queries:
        todo = todo[: args.max_queries]
    log.info("%d searches planned, %d still to do", len(plan), len(todo))

    if args.dry_run:
        for _, name, year in todo[:50]:
            print(f"{name} | {year}")
        if len(todo) > 50:
            print(f"... and {len(todo) - 50} more")
        return

    capped_count = 0
    for number, (code, name, year) in enumerate(todo, 1):
        try:
            if recover and state.done.get(state.key(code, year), {}).get("capped"):
                recover_capped(client, code, name, year, state, records_path,
                               getattr(args, "recovery_budget", 200), getattr(args, "recovery_depth", 3))
                continue
            items, total, capped = fetch_query(client, code, year)
        except Blocked as exc:
            if stop_on_block:
                raise
            log.error("Stopping: %s. Wait, raise --delay, or ask NMC about bulk access.", exc)
            break
        except ApiError as exc:
            log.warning("Skipping %s %s for now: %s", name, year, exc)
            continue
        records = [to_record(item, code, name, year) for item in items]
        append_records(records, records_path)
        state.mark(code, year, len(records), total, capped)
        if capped and recover:
            try:
                recover_capped(client, code, name, year, state, records_path,
                               getattr(args, "recovery_budget", 200), getattr(args, "recovery_depth", 3))
            except Blocked as exc:
                if stop_on_block:
                    raise
                log.error("Stopping recovery: %s", exc)
                break
            except ApiError as exc:
                log.warning("Recovery unfinished for %s %s: %s", name, year, exc)
            continue
        if capped:
            capped_count += 1
            log.warning("CAPPED %s %s: the site holds %d records but serves only %d per search",
                        name, year, total, config.MAX_WINDOW)
        log.info("[%d/%d] %s %s: %d rows (site total %d)", number, len(todo), name, year, len(records), total)
    if capped_count:
        log.warning("%d capped searches this run; see `python scrape.py status`", capped_count)


def run_sample(args) -> None:
    limit = max(1, min(args.limit, config.SAMPLE_MAX))
    client = Client(delay=args.delay)
    matches = [c for c in real_councils(client.states()) if args.council.lower() in c[1].lower()]
    if len(matches) != 1:
        raise SystemExit(f"--council must match exactly one council. Matched: {[m[1] for m in matches] or 'none'}")
    code, name = matches[0]
    items, total, _ = fetch_query(client, code, str(args.year), limit=limit)
    if not items:
        raise SystemExit(f"No records for {name} {args.year}.")

    out = config.DATA_DIR / "sample_full.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:  # overwritten on every run
        for item in items:
            fh.write(json.dumps({**item, "_council": name, "_year": str(args.year)}, ensure_ascii=False) + "\n")

    filled: dict = {}
    for item in items:
        for key, value in item.items():
            if value not in (None, "", [], "N/A", "-"):
                filled[key] = filled.get(key, 0) + 1
    print(f"Wrote {len(items)} of {total} records for {name} {args.year} to {out}")
    print("Fields returned by the API (and how many records have a value):")
    for key in item_keys_in_order(items):
        print(f"  {key:<24} {filled.get(key, 0)}/{len(items)}")
    print("This test file contains personal data (such as date of birth and address).")
    print("Keep it local, do not commit or share it, and delete it when the test is done.")


def run_fullrecord(args) -> None:
    """Fetch one council/year into an overwritten full-record JSONL file."""
    limit = max(1, min(args.limit, config.fullrecord_MAX))
    client = Client(delay=args.delay)
    matches = [c for c in real_councils(client.states()) if args.council.lower() in c[1].lower()]
    if len(matches) != 1:
        raise SystemExit(f"--council must match exactly one council. Matched: {[m[1] for m in matches] or 'none'}")
    code, name = matches[0]
    items, total, _ = fetch_query(client, code, str(args.year), limit=limit)
    if not items:
        raise SystemExit(f"No records for {name} {args.year}.")
    out = config.DATA_DIR / "fullrecord_full.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps({**item, "_council": name, "_year": str(args.year)},
                                ensure_ascii=False) + "\n")
    print(f"Wrote {len(items)} of {total} records for {name} {args.year} to {out}")


def item_keys_in_order(items):
    seen = []
    for item in items:
        for key in item:
            if key not in seen:
                seen.append(key)
    return seen


def _safe_filename(text: str) -> str:
    """Sanitise a council name or year for use in a file name."""
    for ch in r'\/*?:|"<> ':
        text = text.replace(ch, "_")
    return text.strip("_")


def run_collect(args) -> None:
    """Identical flow to run_scrape but keeps config.COLLECT_FIELDS (no allow-list stripping).

    All records are appended to a single file:
      data/collect/collect.jsonl

    Use `export-collect` to split that file into per-council xlsx files, and
    `export-collect-combined` to produce a single xlsx with one sheet per council.

    The 10,000-record cap is handled the same way as `scrape`: recover_capped fires
    registration-prefix filtered searches and deduplicates locally until the site total
    is matched (or the budget/depth limits are reached).  The shared collect.jsonl is
    passed directly as records_path so recover_capped reads and writes the same file;
    dedup inside recover_capped filters by _council_code + _year so mixing councils is safe.

    Uses the same State tracker, append_records, and recover_capped logic so it resumes
    safely and skips already-done pairs.
    """
    client = Client(delay=args.delay)
    state = State(config.DATA_DIR / "state.json")
    collect_dir = config.DATA_DIR / "collect"
    collect_dir.mkdir(parents=True, exist_ok=True)
    records_path = collect_dir / "collect.jsonl"

    try:
        councils = client.states()
        years = client.years()
    except (Blocked, ApiError) as exc:
        log.error("Could not load the council/year lists: %s", exc)
        return

    plan = build_plan(councils, years, args.council, args.year_from, args.year_to)
    recover = getattr(args, "recover_capped", True)
    todo = [p for p in plan if not state.is_done(p[0], p[2])
            or (recover and state.done[state.key(p[0], p[2])].get("capped"))]
    if args.max_queries:
        todo = todo[: args.max_queries]
    log.info("%d searches planned, %d still to do", len(plan), len(todo))

    if args.dry_run:
        for _, name, year in todo[:50]:
            print(f"{name} | {year}")
        if len(todo) > 50:
            print(f"... and {len(todo) - 50} more")
        return

    capped_count = 0
    for number, (code, name, year) in enumerate(todo, 1):
        try:
            if recover and state.done.get(state.key(code, year), {}).get("capped"):
                # Previously capped: resume recovery directly against the shared file.
                # recover_capped reads only rows matching this (code, year) for dedup.
                recover_capped(client, code, name, year, state, records_path,
                               getattr(args, "recovery_budget", 200),
                               getattr(args, "recovery_depth", 3),
                               record_fn=to_record_full, full_records=True)
                continue
            items, total, capped = fetch_query(client, code, year)
        except Blocked as exc:
            log.error("Stopping: %s. Wait, raise --delay, or ask NMC about bulk access.", exc)
            break
        except ApiError as exc:
            log.warning("Skipping %s %s for now: %s", name, year, exc)
            continue

        records = [to_record_full(item, code, name, year) for item in items]
        append_records(records, records_path)
        state.mark(code, year, len(records), total, capped)

        if capped and recover:
            try:
                # Cap hit: use the same registration-prefix filter recovery as scrape.
                # Writes fresh rows directly to the shared collect.jsonl.
                recover_capped(client, code, name, year, state, records_path,
                               getattr(args, "recovery_budget", 200),
                               getattr(args, "recovery_depth", 3),
                               record_fn=to_record_full, full_records=True)
            except Blocked as exc:
                log.error("Stopping recovery: %s", exc)
                break
            except ApiError as exc:
                log.warning("Recovery unfinished for %s %s: %s", name, year, exc)
            continue

        if capped:
            capped_count += 1
            log.warning("CAPPED %s %s: the site holds %d records but serves only %d per search",
                        name, year, total, config.MAX_WINDOW)

        log.info("[%d/%d] %s %s: %d rows (site total %d)", number, len(todo), name, year, len(records), total)

    if capped_count:
        log.warning("%d capped searches this run; see `python scrape.py status`", capped_count)

    if records_path.exists():
        print(f"Collect output: {records_path}")
        print("Run `python scrape.py export-collect` for per-council xlsx files.")
        print("Run `python scrape.py export-collect-combined` for a single combined xlsx.")


def _load_collect_jsonl(src: Path) -> tuple[dict[str, list], list]:
    """Read collect.jsonl and return (by_council, all_fields), deduped.

    by_council maps council name -> list of flattened row dicts.
    all_fields preserves insertion order across all councils.
    """
    seen: set = set()
    by_council: dict[str, list] = {}
    all_fields: list = []
    text = src.read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON array in {src}: {exc}") from exc
        records = list(enumerate(parsed, 1)) if isinstance(parsed, list) else []
    else:
        records = []
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append((line_no, json.loads(line)))
            except json.JSONDecodeError:
                log.warning("Skipping unreadable record %d in %s", line_no, src.name)

    for line_no, record in records:
        if not isinstance(record, dict):
            log.warning("Skipping unreadable record %d in %s", line_no, src.name)
            continue
        ident = json.dumps({k: v for k, v in record.items() if k != "_scraped_at"},
                           sort_keys=True, ensure_ascii=False)
        if ident in seen:
            continue
        seen.add(ident)
        flat = flatten(record)
        council = str(flat.get("_council") or flat.get("state_medical_council") or "Unknown")
        by_council.setdefault(council, []).append(flat)
        for key in flat:
            if key not in all_fields:
                all_fields.append(key)
    return by_council, all_fields


def _write_xlsx(rows: list, all_fields: list, dst: Path, sheet_name: str) -> None:
    """Write a single-sheet xlsx from a list of flat row dicts."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise SystemExit("openpyxl is required: pip install openpyxl")

    sheet_fields = [f for f in all_fields if any(f in r for r in rows)]
    safe_name = sheet_name[:31]
    for ch in r'\/*?:[]]':
        safe_name = safe_name.replace(ch, " ")
    safe_name = safe_name.strip() or "Sheet"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = safe_name

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(fill_type="solid", fgColor="2F5496")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col, field in enumerate(sheet_fields, 1):
        cell = ws.cell(row=1, column=col, value=field)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 30

    for row_idx, record in enumerate(rows, 2):
        for col, field in enumerate(sheet_fields, 1):
            value = record.get(field, "")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            ws.cell(row=row_idx, column=col, value=value if value != "" else None)

    for col, field in enumerate(sheet_fields, 1):
        max_len = len(str(field))
        for row_idx in range(2, min(len(rows) + 2, 202)):
            v = ws.cell(row=row_idx, column=col).value
            if v is not None:
                max_len = max(max_len, min(len(str(v)), 60))
        ws.column_dimensions[get_column_letter(col)].width = max_len + 2

    dst.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dst)
    log.info("Wrote %d rows → %s", len(rows), dst.name)


def _write_xlsx_multi_sheet(by_council: dict[str, list], all_fields: list, dst: Path) -> tuple[int, int]:
    """Write a multi-sheet xlsx (one sheet per council) from pre-loaded data.

    Returns (num_sheets, total_rows).
    """
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise SystemExit("openpyxl is required: pip install openpyxl")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(fill_type="solid", fgColor="2F5496")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    total_rows = 0
    for council in sorted(by_council):
        rows = by_council[council]
        sheet_fields = [f for f in all_fields if any(f in r for r in rows)]

        safe_name = council[:31]
        for ch in r'\/*?:[]]':
            safe_name = safe_name.replace(ch, " ")
        safe_name = safe_name.strip() or "Unknown"
        existing_titles = {s.title for s in wb.worksheets}
        base, suffix = safe_name[:28], 1
        while safe_name in existing_titles:
            safe_name = f"{base}_{suffix}"
            suffix += 1

        ws = wb.create_sheet(title=safe_name)

        for col, field in enumerate(sheet_fields, 1):
            cell = ws.cell(row=1, column=col, value=field)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 30

        for row_idx, record in enumerate(rows, 2):
            for col, field in enumerate(sheet_fields, 1):
                value = record.get(field, "")
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False)
                ws.cell(row=row_idx, column=col, value=value if value != "" else None)

        for col, field in enumerate(sheet_fields, 1):
            max_len = len(str(field))
            for row_idx in range(2, min(len(rows) + 2, 202)):
                v = ws.cell(row=row_idx, column=col).value
                if v is not None:
                    max_len = max(max_len, min(len(str(v)), 60))
            ws.column_dimensions[get_column_letter(col)].width = max_len + 2

        total_rows += len(rows)
        log.info("Sheet %r: %d rows, %d columns", safe_name, len(rows), len(sheet_fields))

    dst.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dst)
    return len(by_council), total_rows


def run_export_collect(args) -> None:
    """Export collect.jsonl into one xlsx file per council, saved to data/collect/."""
    src = config.DATA_DIR / "collect" / "collect.jsonl"
    if not src.exists():
        raise SystemExit(f"Nothing to export yet: {src} does not exist.")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_council, all_fields = _load_collect_jsonl(src)
    if not by_council:
        raise SystemExit("No records found in collect.jsonl")
    total = 0
    for council, rows in sorted(by_council.items()):
        safe = _safe_filename(council) or "Unknown"
        dst = out_dir / f"{safe}.xlsx"
        _write_xlsx(rows, all_fields, dst, sheet_name=council)
        total += len(rows)
        print(f"  {council}: {len(rows)} rows → {dst.name}")
    print(f"Exported {total} rows across {len(by_council)} files to {out_dir}")


def run_export_collect_combined(args) -> None:
    """Export collect.jsonl into a single xlsx with one sheet per council."""
    src = config.DATA_DIR / "collect" / "collect.jsonl"
    if not src.exists():
        raise SystemExit(f"Nothing to export yet: {src} does not exist.")
    by_council, all_fields = _load_collect_jsonl(src)
    if not by_council:
        raise SystemExit("No records found in collect.jsonl")
    dst = Path(args.out)
    sheets, rows = _write_xlsx_multi_sheet(by_council, all_fields, dst)
    print(f"Wrote {rows} unique rows across {sheets} sheets to {dst}")


def run_status(args) -> None:
    path = config.DATA_DIR / "state.json"
    if not path.exists():
        raise SystemExit("No progress recorded yet.")
    done = State(path).done

    def _report(label: str, entries: dict) -> None:
        if not entries:
            return
        empty = sum(1 for v in entries.values() if v["rows"] == 0)
        capped = [k for k, v in entries.items() if v.get("capped")]
        print(f"\n--- {label} ---")
        print(f"{len(entries) - len(capped)} batches complete, {len(capped)} incomplete/capped "
              f"({empty} empty); {sum(v['rows'] for v in entries.values())} rows recorded")
        if capped:
            print(f"{len(capped)} incomplete/capped:")
            for key in capped:
                info = entries[key]
                print(f"  {key.replace('|', ' ')}: saved {info['rows']} of {info['site_total']}")

    # Entries written by `scrape`/`batch` carry no source tag; entries written by `collect`
    # are written to the same state.json (both use State(config.DATA_DIR / "state.json")).
    # We split them by checking for a matching per-pair JSONL in data/collect/.
    collect_dir = config.DATA_DIR / "collect"
    scrape_entries, collect_entries = {}, {}
    for key, value in done.items():
        # A collect entry always has a corresponding per-pair JSONL in data/collect/.
        code, year = key.split("|", 1)
        # Build the same slug that run_collect uses to name the file.
        council_name = value.get("council", "")  # not stored; fall back to file presence check
        candidate = collect_dir / f"collect_*_{_safe_filename(year)}.jsonl"
        import glob as _glob
        pair_files = _glob.glob(str(candidate))
        # Simpler heuristic: if any per-pair file exists whose name ends with the year slug,
        # consider the key a collect entry.  scrape entries have no per-pair files.
        is_collect = bool(pair_files) and any(
            f"_{_safe_filename(year)}.jsonl" in Path(f).name for f in pair_files
        )
        if is_collect:
            collect_entries[key] = value
        else:
            scrape_entries[key] = value

    _report("scrape / batch", scrape_entries)
    _report("collect", collect_entries)


def run_export(args) -> None:
    src = config.DATA_DIR / "imr_records.jsonl"
    if not src.exists():
        raise SystemExit(f"Nothing to export yet: {src} does not exist.")
    import excel_export

    first = getattr(args, "registration_year_from", None)
    last = getattr(args, "registration_year_to", None)
    filtered = first is not None or last is not None
    if getattr(args, "out", None) is None:
        if filtered:
            out = config.DATA_DIR / f"imr_records_registration_{first or 'start'}-{last or 'end'}.xlsx"
        else:
            out = config.DATA_DIR / "imr_records.xlsx"
    else:
        out = Path(args.out)
    stats = excel_export.ExportStats()
    if out.suffix.lower() == ".xlsx":
        result = excel_export.export_xlsx(src, out,
                                          registration_year_from=first,
                                          registration_year_to=last,
                                          stats=stats)
        count = sum(result.values())
    else:
        count = export_csv(src, out, registration_year_from=first,
                           registration_year_to=last, stats=stats)
    if filtered:
        print(f"Unknown/missing registration dates: {stats.unknown_registration_dates}")
        if stats.unknown_registration_dates:
            print("Unknown dates are excluded from the year-filtered export; preserved in source")
        print(f"Registration-year rows exported: {count}")
        state_path = config.DATA_DIR / "state.json"
        if state_path.exists():
            incomplete = [v for v in State(state_path).done.values() if v.get("capped")]
            if incomplete:
                print(f"{len(incomplete)} incomplete/capped source batches; not a complete registration-year total")
                log.warning("Filtered export uses saved records only; do not clear the source state")
            else:
                print("Source coverage: complete")
        else:
            print("Source coverage: unknown (saved records only)")
        print(f"Wrote {count} unique rows to {out}")
    else:
        print(f"Wrote {count} unique rows to {out}")


def export_xlsx(src: Path, dst: Path) -> tuple[int, int]:
    """JSONL -> Excel workbook, one sheet per council, deduped. Returns (sheets, rows)."""
    seen: set = set()
    by_council: dict[str, list] = {}
    all_fields: list = []

    with Path(src).open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Skipping unreadable line %d", line_no)
                continue
            ident = json.dumps({k: v for k, v in record.items() if k != "_scraped_at"},
                               sort_keys=True, ensure_ascii=False)
            if ident in seen:
                continue
            seen.add(ident)
            flat = flatten(record)
            council = str(flat.get("_council") or flat.get("state_medical_council") or "Unknown")
            by_council.setdefault(council, []).append(flat)
            for key in flat:
                if key not in all_fields:
                    all_fields.append(key)

    if not by_council:
        raise SystemExit(f"No records found in {src}")

    return _write_xlsx_multi_sheet(by_council, all_fields, Path(dst))


def run_export_xlsx(args) -> None:
    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"Source not found: {src}")
    dst = Path(args.out)
    sheets, rows = export_xlsx(src, dst)
    print(f"Wrote {rows} unique rows across {sheets} sheets to {dst}")


def run_command(func, args) -> None:
    """Run a command; show Blocked/ApiError as a one-line message instead of a traceback."""
    try:
        func(args)
    except (Blocked, ApiError) as exc:
        raise SystemExit(f"Error: {exc}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="NMC Indian Medical Register collector")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_delay(p):
        p.add_argument("--delay", type=float, default=config.DELAY_SECONDS,
                       help="base seconds between requests (jitter is added)")

    p = sub.add_parser("list", help="show the councils and years the site offers")
    add_delay(p)
    p.set_defaults(func=run_list)

    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--council", action="append", help="part of a council name; repeat for several")
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--info-year-from", dest="year_from", type=int)
    p.add_argument("--info-year-to", dest="year_to", type=int)
    p.add_argument("--max-queries", type=int, help="stop after N (council, year) searches")
    p.add_argument("--dry-run", action="store_true", help="list the searches without running them")
    p.add_argument("--recover-capped", action=argparse.BooleanOptionalAction, default=True,
                   help="automatically recover new and previously capped batches (default: enabled)")
    p.add_argument("--recovery-budget", type=int, default=200,
                   help="maximum new filtered searches per council/year per run (default 200)")
    p.add_argument("--recovery-depth", type=int, default=3,
                   help="maximum registration fragment length (default 3)")
    add_delay(p)
    sub.add_parser("scrape", parents=[p],
                   help="collect the allow-listed fields into data/imr_records.jsonl").set_defaults(func=run_scrape)
    sub.add_parser("batch", parents=[p],
                   help="run each council separately across the selected year range").set_defaults(func=run_batch)
    sub.add_parser("collect", parents=[p],
                   help="all fields (DOB, father's name, etc.) into data/local_records.jsonl").set_defaults(func=run_collect)

    p = sub.add_parser("fullrecord", help="fetch one council/year with every API field")
    p.add_argument("--council", required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--limit", type=int, default=config.fullrecord_DEFAULT,
                   help=f"records to fetch, clamped to 1..{config.fullrecord_MAX}")
    add_delay(p)
    p.set_defaults(func=run_fullrecord)

    p = sub.add_parser("sample", help=f"TEST ONLY: full records, at most {config.SAMPLE_MAX}")
    p.add_argument("--council", required=True, help="part of one council's name")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--limit", type=int, default=config.SAMPLE_DEFAULT)
    add_delay(p)
    p.set_defaults(func=run_sample)

    p = sub.add_parser("status", help="progress and capped searches")
    p.set_defaults(func=run_status)

    p = sub.add_parser("export", help="convert collected JSONL to a de-duplicated CSV")
    p.add_argument("--out", default=None,
                   help="output .xlsx or .csv; defaults to imr_records.xlsx")
    p.add_argument("--registration-year-from", type=int)
    p.add_argument("--registration-year-to", type=int)
    p.set_defaults(func=run_export)

    p = sub.add_parser("export-xlsx",
                       help="export collected JSONL to Excel with one sheet per council")
    p.add_argument("--src", default=str(config.DATA_DIR / "local_records.jsonl"),
                   help="source JSONL file (default: data/local_records.jsonl)")
    p.add_argument("--out", default=str(config.DATA_DIR / "local_records.xlsx"),
                   help="output Excel file (default: data/local_records.xlsx)")
    p.set_defaults(func=run_export_xlsx)

    p = sub.add_parser("export-collect",
                       help="export data/collect/collect.jsonl into one xlsx file per council")
    p.add_argument("--out-dir", default=str(config.DATA_DIR / "collect"),
                   help="output directory for per-council xlsx files (default: data/collect/)")
    p.set_defaults(func=run_export_collect)

    p = sub.add_parser("export-collect-combined",
                       help="export data/collect/collect.jsonl into a single xlsx, one sheet per council")
    p.add_argument("--out", default=str(config.DATA_DIR / "collect" / "collect.xlsx"),
                   help="output xlsx file (default: data/collect/collect.xlsx)")
    p.set_defaults(func=run_export_collect_combined)

    args = parser.parse_args(argv)
    if getattr(args, "recovery_budget", 1) < 1 or getattr(args, "recovery_depth", 1) < 1:
        parser.error("recovery budget and depth must be positive")
    if args.cmd == "export":
        try:
            from year_filters import validate_year_range
            validate_year_range(args.registration_year_from, args.registration_year_to)
        except ValueError as exc:
            parser.error(str(exc))
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(config.DATA_DIR / "run.log", encoding="utf-8")],
    )
    run_command(args.func, args)


if __name__ == "__main__":
    main()
