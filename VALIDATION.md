# V1 information-year / registration-year correction

Checked on 31 October 2026 in `D:\nmc-imr-collector` (the original project).
The separate `v2` project and the original `config.py` were not modified; file hashes
for configuration and principal v2 files were checked before and after this change.

## Changes

- Collection's existing `--year-from` / `--year-to` retain their API information-year
  meaning. Explicit aliases `--info-year-from` / `--info-year-to` are available.
- List/help, batch/collection/recovery logs and status label information-year groups
  explicitly. Existing `_year`, `year_of_info` and checkpoint keys are unchanged.
- Export accepts inclusive `--registration-year-from` / `--registration-year-to`
  bounds. Selection uses complete valid `registration_date` values only.
- Unknown/missing dates stay in the source and unfiltered exports. Filtered exports
  exclude them from matches and report them separately; no information year is inferred.
- XLSX and CSV use the same selection. Existing 14 headers, council sheets, identifier
  text types and qualification variations are preserved.
- Filtered default filenames are separate from the default unfiltered workbook.
- Counts describe saved records only. Coverage and capped information-year checkpoints
  are reported separately and are never cleared by a small filtered export.
- No collection, recovery or raw-record command was run against the live source, and
  this change does not rewrite existing JSONL data or collection state.

## Passing checks

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests -p test_registration_years.py -v` | 19 passed |
| `python -m unittest discover -s tests -p test_export.py -v` | 8 passed |
| Compilation of `scrape.py`, `excel_export.py`, `year_filters.py`, new tests | Passed |
| `python scrape.py export --help` | Passed |
| `python scrape.py scrape --help` | Passed; old/new information-year flags shown |

Checks use synthetic records and temporary output files. No new live bulk collection
or claim of complete registration-year coverage is part of this verification.

## Existing v1 test failures

The complete legacy suite was run **before and after** this correction:

- Before: 74 tests, 2 failures, 3 errors.
- After: 93 tests, the same 2 failures and 3 errors; the 19 new tests all pass.

The unchanged failures/errors are:

1. `SampleTests.test_sample_is_hard_capped`: existing `run_sample` function is absent.
2. `SampleTests.test_sample_keeps_every_field_but_is_small`: same stale function reference.
3. `SampleTests.test_sample_needs_exactly_one_council`: same stale function reference.
4. `BulkTests.test_personal_fields_are_dropped_in_bulk_mode`: the edited v1 field list
   differs from the professional-field schema expected by the original test.
5. `RecoveryTests.test_legacy_capped_batch_recovers_overlapping_and_deeper_filters`:
   the same field-list/test expectation mismatch.

These pre-existing command/configuration differences were not repaired or hidden as
part of the requested year-semantic correction. The complete suite is **not green**.

## Use

Export registration-year 1960 rows from saved bulk data:

```bat
python scrape.py export --registration-year-from 1960 --registration-year-to 1960 --out data/registrations_1960.xlsx
```

This reads `data/imr_records.jsonl` and exports professional fields. An information-year
source group can include many registration years, and later information-year groups
can contain additional registrations from 1960. Export counts are records, not a
guaranteed count of unique practitioners or all registrations made in that year.
