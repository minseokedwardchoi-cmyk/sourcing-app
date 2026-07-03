"""Utilities for normalizing country names stored in the database."""

OTHER_COUNTRY_LABELS = {"기타(ZZ)", "기타 (ZZ)"}


def normalize_country_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return "기타" if cleaned in OTHER_COUNTRY_LABELS else cleaned
