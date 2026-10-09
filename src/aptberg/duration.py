"""Durations as the command line spells them: 7d, 36h, 90m, 3600s"""

from datetime import timedelta

UNITS = {'d': 'days', 'h': 'hours', 'm': 'minutes', 's': 'seconds'}


def parse_duration(text: str) -> timedelta:
    "7d, 36h, 90m or 3600s; ValueError for anything else"
    try:
        return timedelta(**{UNITS[text[-1]]: float(text[:-1])})
    except (KeyError, ValueError, IndexError):
        raise ValueError(f'not a duration like 7d or 36h: {text!r}') from None


def format_duration(delta: timedelta) -> str:
    "The inverse of parse_duration, in the largest whole unit"
    seconds = int(delta.total_seconds())
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if seconds and seconds % size == 0:
            return f'{seconds // size}{unit}'
    return f'{seconds}s'
