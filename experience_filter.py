import re

ASSOCIATE_KEYWORDS = ["associate", "apm"]

FRESHER_KEYWORDS = ["fresher", "entry level", "entry-level", "fresh graduate"]

# Hard ceiling: min experience of 3+ is dropped for every title, no exceptions.
HARD_CUTOFF_MIN_YEARS = 3

_RANGE_PATTERN = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*\+?\s*(?:yrs?|years?)", re.IGNORECASE)
_PLUS_PATTERN = re.compile(r"(\d+)\s*\+\s*(?:yrs?|years?)", re.IGNORECASE)
# Single-value mentions like "1 year(s)" or "2 years" (no range, no "+") — seen on Internshala.
_SINGLE_PATTERN = re.compile(r"(\d+)\s*\+?\s*year", re.IGNORECASE)


def parse_min_experience(text):
    """Extract the minimum years of experience mentioned in free text, or None if unmentioned."""
    if not text:
        return None

    match = _RANGE_PATTERN.search(text)
    if match:
        return int(match.group(1))

    match = _PLUS_PATTERN.search(text)
    if match:
        return int(match.group(1))

    match = _SINGLE_PATTERN.search(text)
    if match:
        return int(match.group(1))

    return None


def extract_experience_range(text):
    """Return the raw matched experience-range substring (e.g. '1-3 years'), or
    'Not specified' if no experience is mentioned in the text."""
    if not text:
        return "Not specified"

    match = _RANGE_PATTERN.search(text)
    if match:
        return match.group(0).strip()

    match = _PLUS_PATTERN.search(text)
    if match:
        return match.group(0).strip()

    match = _SINGLE_PATTERN.search(text)
    if match:
        return match.group(0).strip()

    return "Not specified"


def is_associate_title(title):
    lowered = (title or "").lower()
    return any(re.search(rf"\b{keyword}\b", lowered) for keyword in ASSOCIATE_KEYWORDS)


def has_fresher_signal(text):
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in FRESHER_KEYWORDS)


def has_product_in_title(title):
    return "product" in (title or "").lower()


def get_tier(min_years, text):
    """
    Tier 1 (always keep, highest priority): 0-1 yrs, fresher/entry-level/fresh-grad
    signal, or no experience mentioned at all.
    Tier 2 (keep): min experience of 1 or 2 yrs, for ANY title, that isn't already Tier 1.
    Drop (returns None): min experience of 3+ yrs, for every title.
    """
    if min_years is None or min_years == 0 or has_fresher_signal(text):
        return 1

    if min_years >= HARD_CUTOFF_MIN_YEARS:
        return None

    return 2  # min_years is 1 or 2


def is_experience_allowed(min_years, title):
    return get_tier(min_years, title) is not None
