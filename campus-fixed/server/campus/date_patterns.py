"""Pattern declarations reused from Touxian triage (MIT); no desktop CLI or I/O."""

import re

# The day alternation reads ``[0-2]?\d`` before ``3[01]``, so a bare ``3`` wins
# over ``30``/``31`` unless something stops the match there. The yearless pattern
# below has always carried ``(?!\d)``; the numeric-year pattern did not, and
# ``2026.11.30`` matched as ``2026.11.3`` -- a valid date, at date precision,
# silently 27 days early. Keep the boundary on both.
DATE_PATTERNS = [
    re.compile("(?P<year>20\\d{2})\\s*年\\s*(?P<month>\\d{1,2})\\s*月\\s*(?P<day>\\d{1,2})\\s*[日号]?"),
    re.compile("(?P<year>20\\d{2})[./-](?P<month>0?[1-9]|1[0-2])[./-](?P<day>[0-2]?\\d|3[01])(?!\\d)"),
    re.compile("(?P<month>\\d{1,2})\\s*月\\s*(?P<day>\\d{1,2})\\s*[日号]?"),
    re.compile("(?<!\\d)(?P<month>0?[1-9]|1[0-2])[./-](?P<day>[0-2]?\\d|3[01])(?!\\d)"),
]

WEEKDAY_PATTERN = re.compile("(?:(?P<prefix>本|这|下)周|周|星期)(?P<weekday>[一二三四五六日天])(?P<before>前|之前)?")

TIME_PATTERN = re.compile("(?P<hour>\\d{1,2})\\s*[:：.]\\s*(?P<minute>\\d{2})")

HOUR_PATTERN = re.compile("(?P<hour>\\d{1,2})点(?P<half>半)?")
