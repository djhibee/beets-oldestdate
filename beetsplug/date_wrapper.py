import datetime
import re
from typing import Optional

from dateutil import parser


class DateWrapper(datetime.datetime):
    """
    Wrapper class for datetime objects.
    Allows comparison between dates,
    with the month and day being optional.
    """

    def __new__(cls, y: Optional[int] = None, m: Optional[int] = None, d: Optional[int] = None,
                iso_string: Optional[str] = None) -> 'DateWrapper':
        """
        Create a new datetime object using a convenience wrapper.
        Must specify at least one of either year or iso_string.
        :param y: The year, as an integer
        :param m: The month, as an integer (optional)
        :param d: The day, as an integer (optional)
        :param iso_string: A string representing the date in the format YYYYMMDD. Month and day are optional.
        """
        if y is not None:
            year = min(max(y, datetime.MINYEAR), datetime.MAXYEAR)
            month = m if (m is not None and 0 < m <= 12) else 1
            day = d if (d is not None and 0 < d <= 31) else 1
        elif iso_string is not None:
            # Replace question marks with first valid field
            iso_string = iso_string.replace("??", "01")

            parsed = parser.isoparse(iso_string)
            return datetime.datetime.__new__(cls, parsed.year, parsed.month, parsed.day)
        else:
            raise TypeError("Must either specify a value for year, or a date string")

        return datetime.datetime.__new__(cls, year, month, day)

    @classmethod
    def today(cls) -> 'DateWrapper':
        today = datetime.date.today()
        return DateWrapper(today.year, today.month, today.day)

    def __init__(self, y: Optional[int] = None, m: Optional[int] = None, d: Optional[int] = None,
                 iso_string: Optional[str] = None) -> None:
        if y is not None:
            self.y = min(max(y, datetime.MINYEAR), datetime.MAXYEAR)
            self.m = m if (m is None or 0 < m <= 12) else 1
            self.d = d if (d is None or 0 < d <= 31) else 1
        elif iso_string is not None:
            # Remove any hyphen separators
            iso_string = iso_string.replace("-", "")
            length = len(iso_string)

            if length < 4:
                raise ValueError("Invalid value for year")

            self.y = int(iso_string[:4])
            self.m = None
            self.d = None

            # Month and day are optional. Sometimes fields are missing or contain ??
            if length >= 6:
                try:
                    self.m = int(iso_string[4:6])
                except ValueError:
                    pass
                if length >= 8:
                    try:
                        self.d = int(iso_string[6:8])
                    except ValueError:
                        pass

        else:
            raise TypeError("Must specify a value for year or a date string")

    def __lt__(self, other: datetime.date) -> bool:
        if not isinstance(other, DateWrapper):
            return NotImplemented

        if self.y != other.y:
            return self.y < other.y
        elif self.m is None:
            return False
        else:
            if other.m is None:
                return True
            elif self.m == other.m:
                if self.d is None:
                    return False
                else:
                    if other.d is None:
                        return True
                    else:
                        return self.d < other.d
            else:
                return self.m < other.m

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DateWrapper):
            return NotImplemented

        if self.y != other.y:
            return False
        elif self.m is not None and other.m is not None:
            if self.d is not None and other.d is not None:
                return self.d == other.d
            else:
                return self.m == other.m
        else:
            return self.m == other.m


# MusicBrainz stores partially known dates with ``??`` (or ``00`` in older data) for unknown components.
_PARTIAL_DATE_RE = re.compile(r'^\s*(?P<year>\d{4})(?:-(?P<month>\d{2}|\?\?)(?:-(?P<day>\d{2}|\?\?))?)?\s*$')


def parse_musicbrainz_date(value: Optional[str]) -> Optional[DateWrapper]:
    """
    Parse a full or partial MusicBrainz date (YYYY, YYYY-MM, YYYY-MM-DD, with ``??``/``00`` for unknown parts).
    Unknown month or day are never invented: ``2015-??-??`` and ``2015-??-13`` are year-only, ``2015-07-??`` is
    year and month only. Returns None when the date has no usable year or invalid components.
    """
    if not value:
        return None
    match = _PARTIAL_DATE_RE.match(str(value))
    if not match:
        return None

    year = int(match.group('year'))
    if not datetime.MINYEAR <= year <= datetime.MAXYEAR:
        return None

    month: Optional[int] = None
    day: Optional[int] = None
    month_text = match.group('month')
    day_text = match.group('day')
    if month_text not in (None, '??', '00'):
        month = int(month_text)
        if not 1 <= month <= 12:
            return None
        # A day without a known month is not a usable level of precision
        if day_text not in (None, '??', '00'):
            day = int(day_text)
            try:
                datetime.date(year, month, day)
            except ValueError:  # Impossible calendar date such as 2015-02-30
                return None

    return DateWrapper(year, month, day)
