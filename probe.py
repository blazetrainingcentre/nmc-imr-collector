"""Read-only probe: how do the register's name and registration-number filters behave?

Usage
    python probe.py [--council "Tamil Nadu"] [--year 2025] [--delay 1.5]

It makes at most 9 requests (pages of one search, then a few filtered searches) and prints
ONLY counts and yes/no answers. No names or other personal values are printed or saved.

What it answers
    * Is the server's paging order stable (so pages neither overlap nor skip rows)?
    * Does the `name` filter match the START of a name, the start of any word, or anywhere?
    * Does the `reg_no` filter match the START of a number, or anywhere in it?
    * Do the filters combine with the year and council (needed to split a capped search)?
Those answers decide how a search larger than the site's 10,000-result window can be split.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import config
from imr_client import ApiError, Blocked, Client
from scrape import real_councils


def fractions(rows, field, term):
    """How many returned rows have `term` at the start / at the start of a word / anywhere."""
    term = term.lower()
    out = {"n": len(rows), "starts": 0, "word": 0, "contains": 0}
    for row in rows:
        value = str(row.get(field, "")).lower()
        if value.startswith(term):
            out["starts"] += 1
        if any(w.startswith(term) for w in re.split(r"[^a-z0-9]+", value) if w):
            out["word"] += 1
        if term in value:
            out["contains"] += 1
    return out


def classify_name(start_rows, start_total, mid_rows, mid_total, start_term, mid_term):
    """Decide how the text filter matches. `mid_term` is taken from the MIDDLE of a real name."""
    if start_total is None:
        return "rejected"
    if start_total == 0:
        return "exact only"
    start = fractions(start_rows, "name", start_term)
    if start["contains"] < start["n"]:
        return "unclear"
    if mid_total is None:
        return "unclear"
    if mid_total == 0:
        if start["starts"] == start["n"]:
            return "starts-with"
        if start["word"] == start["n"]:
            return "word-start"
        return "unclear"
    mid = fractions(mid_rows, "name", mid_term)
    return "starts-with" if mid["starts"] == mid["n"] else "contains"


def classify_reg(prefix_rows, prefix_total, mid_rows, mid_total, prefix_term, mid_term, exact_ok):
    if prefix_total is None:
        return "rejected"
    if prefix_total == 0:
        return "exact only" if exact_ok else "unclear"
    prefix = fractions(prefix_rows, "registration_no", prefix_term)
    if prefix["starts"] < prefix["n"]:
        return "contains" if prefix["contains"] == prefix["n"] else "unclear"
    if mid_total is None:
        return "unclear"
    if mid_total == 0:
        return "starts-with"
    mid = fractions(mid_rows, "registration_no", mid_term)
    return "starts-with" if mid["starts"] == mid["n"] else "contains"


def local_counts(data_dir: Path, code: str, year: str):
    """(rows, unique registration numbers) already saved for this search, if any."""
    path = Path(data_dir) / "imr_records.jsonl"
    if not path.exists():
        return None
    regs = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("_council_code") == code and str(rec.get("_year")) == str(year):
                regs.append(str(rec.get("registration_no", "")))
    return (len(regs), len(set(regs))) if regs else None


def yes(flag) -> str:
    return "yes" if flag else "NO"


def run_probe(client, code, council, year, out=print, data_dir=None):
    year = str(year)
    res = {"requests": 0}

    def ask(page=1, name="", reg_no=""):
        """One request. Returns (rows, total), or (None, None) if the server rejects the filter."""
        res["requests"] += 1
        try:
            response = client.search(code, year, page, config.PER_PAGE, name=name, reg_no=reg_no)
        except ApiError:
            return None, None
        rows = response.get("data") or []
        total = int((response.get("pagination") or {}).get("total") or len(rows))
        return rows, total

    out(f"Probe: {council} {year}. Read-only; only counts are printed, no names or personal values.")

    # 1-3: paging behaviour with no filters
    page1, total = ask(1)
    if not page1:
        raise SystemExit("That council and year has no records. Pick another with --council / --year.")
    regs1 = [str(r.get("registration_no", "")) for r in page1]
    res["site_total"] = total
    out(f"[1] No filter: site total {total}; page 1 has {len(page1)} rows, {len(set(regs1))} unique registration numbers")
    if total > len(page1):
        page2, _ = ask(2)
        regs2 = {str(r.get("registration_no", "")) for r in (page2 or [])}
        res["paging_overlap"] = len(set(regs1) & regs2)
        out(f"[2] Pages 1 and 2 share {res['paging_overlap']} rows (should be 0)")
    again, _ = ask(1)
    res["stable_order"] = [str(r.get("registration_no", "")) for r in (again or [])] == regs1
    out(f"[3] Repeating page 1 gives the same rows in the same order: {yes(res['stable_order'])}")

    # pick a real record to build test terms from (terms are never printed)
    source = next((r for r in page1
                   if len(str(r.get("registration_no", ""))) >= 4
                   and any(len(w) >= 4 for w in re.findall(r"[A-Za-z]+", str(r.get("name", ""))))), None)
    if source is None:
        out("Could not find a record with a usable name and registration number to test with.")
        res.update(name_mode="unclear", reg_mode="unclear")
        return res
    word = next(w for w in re.findall(r"[A-Za-z]+", str(source["name"])) if len(w) >= 4).lower()
    reg = str(source["registration_no"])

    # 4-6: the name filter
    start_term = word[0]
    start_rows, start_total = ask(name=start_term)
    if start_total is None:                      # maybe a minimum length applies
        start_term = word[:3]
        start_rows, start_total = ask(name=start_term)
    mid_term = word[1:4]
    upper_rows, upper_total = ask(name=start_term.upper())
    mid_rows, mid_total = ask(name=mid_term)
    res["name_mode"] = classify_name(start_rows or [], start_total, mid_rows or [], mid_total, start_term, mid_term)
    res["name_case_insensitive"] = (start_total is not None and start_total == upper_total)
    if start_total is not None:
        ratio = 100 * start_total / total if total else 0
        out(f"[4] name filter, {len(start_term)}-letter term: {start_total} matches ({ratio:.1f}% of the search)")
        out(f"[5] same term in capital letters gives the same count: {yes(res['name_case_insensitive'])}")
    else:
        out("[4] name filter: the server rejected short terms")
    out(f"[6] name filter with a fragment taken from the middle of a name: {mid_total} matches")
    out(f"    => name filter behaves as: {res['name_mode'].upper()}")

    # combination with year/council (uses rows from the name filter)
    checked = start_rows or []
    res["filters_combine"] = bool(checked) and all(
        str(r.get("year_of_info", "")) == year
        and str(r.get("state_medical_council", "")).strip().lower() == council.strip().lower()
        for r in checked)
    out(f"    name filter keeps results inside the chosen year and council: {yes(res['filters_combine'])}")

    # 7-9: the registration-number filter
    prefix_term, mid_reg_term = reg[:3], reg[1:4]
    prefix_rows, prefix_total = ask(reg_no=prefix_term)
    mid_rows2, mid_total2 = ask(reg_no=mid_reg_term)
    exact_rows, exact_total = ask(reg_no=reg)
    exact_ok = exact_total == 1 and any(str(r.get("registration_no", "")) == reg for r in (exact_rows or []))
    res["reg_mode"] = classify_reg(prefix_rows or [], prefix_total, mid_rows2 or [], mid_total2,
                                   prefix_term, mid_reg_term, exact_ok)
    res["reg_exact_ok"] = exact_ok
    out(f"[7] reg_no filter with the first 3 characters: {prefix_total} matches")
    out(f"[8] reg_no filter with 3 characters from the middle: {mid_total2} matches")
    out(f"[9] reg_no filter with a whole number returns exactly that one record: {yes(exact_ok)}")
    out(f"    => reg_no filter behaves as: {res['reg_mode'].upper()}")

    # verdict
    if res["reg_mode"] == "starts-with" and res["filters_combine"]:
        res["recommendation"] = "Split a capped search by registration-number prefix (digit by digit)."
    elif res["name_mode"] == "starts-with" and res["filters_combine"]:
        res["recommendation"] = "Split a capped search by name prefix (letter by letter)."
    elif "contains" in (res["name_mode"], res["reg_mode"]) and res["filters_combine"]:
        res["recommendation"] = ("The filters match anywhere in the value, so pieces overlap. "
                                 "Use fragments with de-duplication by registration number.")
    else:
        res["recommendation"] = "No dependable split found. Ask NMC for the dataset instead."
    if not res["stable_order"] or res.get("paging_overlap"):
        res["recommendation"] += (" Warning: the server's paging order looks unstable, "
                                  "so every split must be checked against the site total.")
    out(f"VERDICT: {res['recommendation']}")

    local = local_counts(data_dir or config.DATA_DIR, code, year)
    if local:
        res["local"] = local
        out(f"[local] Your saved data for this search: {local[0]} rows, {local[1]} unique registration numbers"
            + ("" if local[0] == local[1] else "  <- duplicates mean paging was not fully stable"))
    out(f"({res['requests']} requests made)")
    return res


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Probe how the register's filters behave (read-only)")
    parser.add_argument("--council", default="Tamil Nadu", help="part of one council's name")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--delay", type=float, default=config.DELAY_SECONDS)
    args = parser.parse_args(argv)

    client = Client(delay=args.delay)
    try:
        matches = [c for c in real_councils(client.states()) if args.council.lower() in c[1].lower()]
        if len(matches) != 1:
            raise SystemExit(f"--council must match exactly one council. Matched: "
                             f"{[m[1] for m in matches] or 'none'}")
        code, name = matches[0]
        run_probe(client, code, name, args.year)
    except (Blocked, ApiError) as exc:
        raise SystemExit(f"Error: {exc}")


if __name__ == "__main__":
    main()
