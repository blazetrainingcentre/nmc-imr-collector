"""Registration-date interpretation, independent of the API's information-year filter."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache


def validate_year_range(year_from=None, year_to=None):
    for value in (year_from, year_to):
        if value is not None and (type(value) is not int or not 1 <= value <= 9999):
            raise ValueError("Registration years must be integers from 1 through 9999")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("Registration-year start must not exceed registration-year end")


def registration_year(value):
    """Return the year of a complete, valid source date, or None.

    Numeric day-first dates follow the register's Indian convention. Year-only,
    two-digit-year and unrecognized values remain unknown; year_of_info is never
    consulted. ISO timestamps retain their written local calendar year.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    return _parse_year(value.strip())


@lru_cache(maxsize=4096)
def _parse_year(value):
    try:
        # fromisoformat also accepts a compact YYYYMMDD value on recent Python;
        # require the explicit YYYY-MM-DD form for consistent supported versions.
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return datetime.fromisoformat(value.replace("Z", "+00:00")).year
    except ValueError:
        pass
    for pattern in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d",
                    "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).year
        except ValueError:
            pass
    return None
