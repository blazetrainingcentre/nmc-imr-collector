# NMC Indian Medical Register collector (original project / v1)

Collects listings from the public
[Indian Medical Register](https://nmc.org.in/information-desk/indian-medical-register)
by calling the same JSON endpoints the register page itself uses (councils, years, search),
100 records per request. The collector uses Python 3.9+; Excel export and optional
Google Drive synchronization install the packages listed in `requirements.txt`.

> **Status:** tested against a local *mock* of the page's JSON API, built from the page's own
> script. It has not been run against nmc.org.in. Start with the small test steps below.

## Read this first

**Terms of use.** The NMC website disclaimer permits copying its content for non-commercial
use by you or within your organisation; any other use, including republication, needs
NMC's written permission. Keep collected data **local**. `data/`, `*.jsonl` and `*.csv` are
git-ignored; do not force-add them. This repository should hold **code only**.

**Personal data.** The register lists named individuals, and each record also carries date of
birth, father/husband name and a permanent address.
- `scrape` (bulk) keeps only the fields in `ALLOWED_FIELDS` in `config.py`: name, registration
  number/date, council, year of info, UPRN, qualification(s), qualification year, university.
  The rest is dropped before anything is written.
- `sample` (test) keeps **every** field, but never more than 500 records, so you can see the
  full record shape and check the pipeline. Treat `data/sample_full.jsonl` as sensitive: keep it
  local, do not share it, delete it after testing.
- `collect` follows the same paging, resume and capped-recovery flow as `scrape`, but preserves
  every field returned by the API in `data/collect/collect.jsonl`. Treat it as sensitive.

**Be a good client.** Sequential requests with jittered pauses (default about 1.5-2.7 s).
HTTP 403/429, or a reply that is not JSON, **stops the run**; nothing here tries to get
around that. Optionally set `IMR_CONTACT` to your email so it is included in the User-Agent.

## Run (Windows: use `python`; Mac/Linux: `python3`)

Optional but recommended on Windows: `pip install truststore` (see Troubleshooting).

```
python scrape.py list                                                  # councils and years
python scrape.py sample --council "Tamil Nadu" --year 2025 --limit 100  # TEST: full records
python scrape.py scrape --council "Tamil Nadu" --year-from 2025 --year-to 2025
python scrape.py collect --council "Tamil Nadu" --year-from 2025 --year-to 2025
python scrape.py status                                                # progress, capped searches
python scrape.py export                                                # data/imr_records.xlsx, sheets by council
python scrape.py export --out data/imr_records.csv                     # optional flat CSV
```

The bulk command is unattended and year-major: it completes every selected council for
one year, then advances to the next year. By default it processes every year and council
offered by the site. Use `--year-from`/`--year-to` and repeatable `--council` to narrow the
run. Other options: `scrape --dry-run` (list searches only), `--max-queries N`, `--delay 3`.

## Information year is not registration year

The API's year parameter selects `year_of_info`. It does **not** restrict
`registration_date` to that calendar year. `--year-from` and `--year-to` retain their
existing information-year meaning; the clearer aliases are `--info-year-from` and
`--info-year-to`. Existing data and checkpoints do not need to be migrated.

For example, Tamil Nadu's 1960 information-year search reported 11,517 source rows.
The inspected 10,000-row portion contained registration dates spanning 1915-1960,
including 617 rows with registration dates in 1960. Neither 11,517 nor the partial
617 count establishes the complete number of registrations made in 1960.

To filter saved professional records by actual registration-date year:

```bat
python scrape.py export --registration-year-from 1960 --registration-year-to 1960 --out data/registrations_1960.xlsx
python scrape.py export --format csv --registration-year-from 1960 --registration-year-to 1960
```

Bounds are inclusive and either can be omitted. These filters affect only the export;
they do not fetch records, rewrite JSONL data, alter checkpoints or change recovery
counts. Filtering always uses `registration_date`; it never substitutes `year_of_info`,
`_year` or qualification year. Supported complete dates include ISO dates/timestamps
and day-first `DD-MM-YYYY`, `DD/MM/YYYY`, `DD.MM.YYYY` forms. Missing, invalid, partial
and year-only dates remain unknown. They stay in the source and in unfiltered exports;
year-filtered exports exclude them and report their count separately.

Exports print matching, out-of-range, unknown-date and unreadable-line counts, along
with information-year checkpoint coverage. A small matching count never clears a
capped source batch. Registration-year results describe the saved records, not a
guaranteed complete cohort: other information-year groups may contain more matches.
Distinct qualification entries remain distinct records, not necessarily distinct people.

Without `--out`, filtered exports use a separate name such as
`data/imr_records_registration_1960-1960.xlsx`, leaving the default unfiltered workbook
alone. The original 14 headers and council tabs remain unchanged. In those headers,
both `Info YR` and metadata `Year` refer to information years; `Date` is the registration
date used for this filter.

### Windows batch launcher: all councils, 1960-2026

Double-click `run_all_councils_1960_2026.bat`, or run it from a terminal:

```bat
run_all_councils_1960_2026.bat
```

It uses the project's `.venv` automatically when present and calls `scrape.py batch`.
The batch command discovers councils and explicitly selects one council at a time,
running that council's entire available 1960-2026 range in ascending year order before
starting the next council. It enables capped-batch recovery and shows status when the
collector stops. Run the same file again to resume. Years the site
does not offer cannot be fetched. Existing pacing, recovery budgets and stop-on-block
behavior apply; there is no automatic restart loop after a site refusal.

Extra scraper options can be passed through, for example:

```bat
run_all_councils_1960_2026.bat --dry-run
run_all_councils_1960_2026.bat --delay 3
```

The launcher pauses at the end so its summary remains visible when double-clicked.

## What to know

- **10,000-record window.** The site serves at most 10,000 results per search. A council and
  year with more than that is saved up to the limit and flagged "capped" in `data/state.json`
  (see `python scrape.py status`). Both `scrape` and `batch` automatically try narrower
  registration searches and recover the remaining rows with deduplication. Previously
  capped council/year batches are also revisited when you rerun the same command.
  Use `--no-recover-capped` only to disable this automatic recovery.
- **Resuming.** Each council+year search is saved in one step, then recorded in
  `data/state.json`. Stop any time (Ctrl+C) and run the same command again.
- **Empty searches** (a council with nothing registered that year) finish in one request.
- **Pacing.** About 90 requests per 9,000 rows. The full register is very large: run it in
  slices (`--council`, `--year-from/--year-to`) over several sessions.
- **Output.** `data/imr_records.jsonl`, one record per line, plus `_council_code`,
  `_council`, `_year`, `_scraped_at`. `export` writes a de-duplicated Excel workbook with
  one sheet per council and flattens additional qualifications into one column.

## Export to Excel by council

```bat
python scrape.py export
python scrape.py export --out data/all_councils.xlsx
python scrape.py export --format csv
python scrape.py export --out data/imr_records.csv
```

The default workbook is `data/imr_records.xlsx`. Tamil Nadu is named `TN`, Karnataka
`KAN`; other councils use their saved council code (or name when a code is unavailable).
Every sheet includes all saved years for that council. Headers, in order, are:

`Info YR`, `Reg No`, `Coucil`, `Name`, `Date`, `UPRN No`, `Qualification`,
`Yr of Qualification`, `University`, `Additional`, `Coucil ID`, `Coucil`, `Year`, `Scrapped @`.

The first `Coucil` is the council label returned in the source record; the second is
the council selected for collection. These spellings and duplicate headings follow the
requested export layout. Registration numbers, UPRNs and council IDs remain text, so
leading zeros are preserved. Recognized dates and years are sortable Excel values;
timestamps display in UTC. Partial/unrecognized dates stay as their original text.
Header rows are frozen and filters enabled. Additional qualifications appear as
`qualification | year | university`, separated by semicolons.

Both formats remove exact repeated records while ignoring the scraping timestamp;
distinct qualification/university entries are kept. Export is local and makes no web
requests. It uses the standard library and streams worksheet data to temporary disk
files, so no extra packages are needed. A council exceeding Excel's 1,048,575 data-row
limit continues on sheets such as `TN_2`. Unsupported cell text longer than 32,767
characters fails with a message instead of silently truncating it. An existing workbook
is replaced only after a successful export. Close it in Excel before exporting again.
Incomplete/capped batches are included as currently saved, with a warning in the terminal.

## Probe: splitting searches the site caps

If `status` shows a capped search, the missing rows can only be reached by narrowing the
search with the form's own filters (name, registration number). First find out how those
filters behave:

```
python probe.py --council "Tamil Nadu" --year 2025
```

It is read-only, makes at most 9 requests and prints only counts and yes/no answers (no names
or other personal values). The verdict says whether a split by registration-number prefix, by
name prefix, or by overlapping fragments is possible, and whether the site's paging order is
stable. A user-run probe for Tamil Nadu 2025 reported contains matching for both fields,
stable paging, and 12,449 results, with 10,000 saved locally.

## Recover capped batches with contains filters

Recovery is enabled by default. After confirming registration filtering and stable
paging with the probe, run (the existing `--recover-capped` flag also remains supported):

```
python scrape.py scrape --council "Tamil Nadu" --year-from 2025 --year-to 2025
python scrape.py status
```

This revisits previously capped batches (even though older state files marked them done),
reuses saved rows, and adds only new identities. It searches registration fragments `0`-`9`
and `a`-`z`. A fragment above the window is extended into longer fragments. All searches
retain the selected council and year. Overlap is deduplicated within that council/year
using all allow-listed record fields. Entries sharing a registration number, name and
registration date but differing in qualification or university are preserved as distinct
source records. Scraping timestamps and private fields do not affect deduplication.
Older fragment checkpoints are automatically rechecked once using this corrected identity;
saved records are retained.

The live API ignores the literal registration filter `0`, returning unfiltered results.
Recovery skips that request and expands directly to `00`-`09` and `0a`-`0z` (requires
`--recovery-depth` of at least 2). Other filters still undergo strict council, year and
registration validation; failures report mismatch counts without printing personal values.

Each finished fragment is checkpointed in `data/state.json`. Rerun the same command to
resume. By default each batch gets at most 200 new filtered searches per invocation,
with fragments up to three characters long. Each search can involve multiple page requests.
To continue with a larger search budget or depth:

```
python scrape.py scrape --council "Tamil Nadu" --year-from 2025 --year-to 2025 --recover-capped --recovery-budget 400 --recovery-depth 4
```

For unattended collection across all selected councils and years, recovery runs
automatically with your usual `scrape` or `batch` command. Requests keep the existing pauses and stop on 403/429 or
challenge responses.

Contains fragments overlap and are **not exhaustive partitions**. Missing, very short,
punctuated or non-ASCII registration values, unstable paging, duplicate source identities,
or budget/depth limits can leave gaps. A batch clears its capped flag only when its local
unique count equals the unfiltered site total, with that total checked again at the end.
An equal count is a consistency check, not a snapshot guarantee: source changes that keep
the same total cannot be detected this way. Incomplete batches stay visibly flagged;
exhausting a queue never implies completion. If the total changed since the original
collection, recovery stops for that batch; use a fresh `IMR_DATA_DIR` for a new snapshot.
The recovery code has been verified with offline tests; a live recovery has not been run
as part of this change. Keep existing files; no deletion or state reset is required.

## Troubleshooting

## Run on GitHub Actions with Google Drive

The repository includes a scheduled/manual workflow and Drive sync utility.
Follow [CLOUD_SETUP.md](CLOUD_SETUP.md) to configure private Google Drive
storage, GitHub Actions secrets, and resumable cloud collection.

**`CERTIFICATE_VERIFY_FAILED` / "unable to get local issuer certificate".**
Python could not build a trusted certificate chain for nmc.org.in. Browsers hide this kind of
problem by fetching missing intermediate certificates themselves; Python does not. The tool
fails immediately instead of retrying, and it deliberately has **no switch to turn certificate
checking off**. Try, in order:

1. `pip install truststore`, then run again. Python then uses the Windows certificate store,
   as your browser does.
2. If it still fails, antivirus "HTTPS scanning" or a company proxy may be re-signing traffic.
   Exclude `python.exe` from HTTPS scanning, or export that scanner's root certificate.
3. Export the missing certificate from your browser (padlock > certificate > Details > Export,
   "Base-64 encoded X.509 (.CER)") and point the tool at it:
   `set IMR_CA_BUNDLE=C:\certs\nmc-chain.cer` (Windows `cmd`), then run again.

**HTTP 403/429, or "returned text/html instead of JSON".** The site is refusing automated
requests. The run stops by design. Wait, raise `--delay`, or ask NMC about bulk access.

## Tests

```
python -m unittest discover -s tests -v
```

They run against a local mock server and cover paging, the 10,000 window, empty searches,
the dropped personal fields, resume, retry on a 500, stopping on 429 or an HTML reply, and
that certificate checking stays on.

## Layout

```
config.py        settings, allow-listed fields, limits
requirements.txt optional: truststore (Windows certificate store)
imr_client.py    the JSON client (stdlib only)
scrape.py        list / scrape / sample / status / export
probe.py         read-only check of the name / reg_no filters
tests/           offline tests with a mock server
data/            output and state (git-ignored, created at runtime)
```
