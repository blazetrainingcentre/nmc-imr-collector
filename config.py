"""Settings for the NMC Indian Medical Register (IMR) collector.

This version reads the same JSON endpoints that the register page itself calls
(states, years, search). No browser and no third-party packages are needed.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

BASE = os.environ.get("IMR_BASE", "https://nmc.org.in/indian-medical-register").rstrip("/")
PAGE_URL = "https://nmc.org.in/information-desk/indian-medical-register"
DATA_DIR = Path(os.environ.get("IMR_DATA_DIR", ROOT / "data"))  # git-ignored

PER_PAGE = 100            # the site's own per-request ceiling
MAX_WINDOW = 10_000       # the site serves at most page * per_page <= 10,000 per search
DELAY_SECONDS = 1.5       # base pause between requests (random jitter is added)
TIMEOUT_SECONDS = 30
MAX_RETRIES = 3

SAMPLE_DEFAULT = 100
SAMPLE_MAX = 500          # `sample` is a test tool: full records, small and capped

# `fullrecord` is a bounded diagnostic command.  It is deliberately limited to
# the site's single-search window, while the `collect` command remains the
# resumable, uncapped bulk command.
fullrecord_MAX = min(MAX_WINDOW, int(os.environ.get("IMR_FULLRECORD_MAX", MAX_WINDOW)))
fullrecord_DEFAULT = int(os.environ.get("IMR_FULLRECORD_DEFAULT", MAX_WINDOW))
fullrecord_DEFAULT = max(1, min(fullrecord_DEFAULT, fullrecord_MAX))

# Bulk runs (scrape/batch) keep ONLY these fields. Anything else the API returns
# (date of birth, father/husband name, permanent address, ...) is dropped before writing.
ALLOWED_FIELDS = (
    "year_of_info",
    "registration_no",
    "state_medical_council",
    "name",
    "registration_date",
    "uprn_no",
    "qualification",
    "qualification_year",
    "university",
    "additional_qualifications",
)
ALLOWED_ADDITIONAL = ("qualification", "year", "university")

# collect command keeps ALL of these fields (no stripping).
# Add or remove field names here to control what ends up in local_records.jsonl.
COLLECT_FIELDS = (
    "year_of_info",
    "registration_no",
    "state_medical_council",
    "name",
    "father_name",
    "date_of_birth",
    "mobile_no",
    "registration_date",
    "uprn_no",
    "qualification",
    "qualification_year",
    "university",
    "additional_qualifications",
    "permanent_address",
)

# Optional: put your email in IMR_CONTACT so NMC can reach you about the traffic.
CONTACT = os.environ.get("IMR_CONTACT", "").strip()
USER_AGENT = "nmc-imr-collector/2.0 (non-commercial research" + (f"; {CONTACT}" if CONTACT else "") + ")"
