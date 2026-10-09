import datetime
import unittest
from beetsplug.date_wrapper import DateWrapper, parse_musicbrainz_date


class DateWrapperTest(unittest.TestCase):
    def test_creating_date(self):
        result = DateWrapper(2022, 12, 10)
        self.assertEqual(2022, result.y)
        self.assertEqual(12, result.m)
        self.assertEqual(10, result.d)

    def test_invalid_date(self):
        result = DateWrapper(2022, 0, 10)
        self.assertEqual(2022, result.y)
        self.assertEqual(1, result.m)
        self.assertEqual(10, result.d)
        result = DateWrapper(2022, 10, 0)
        self.assertEqual(2022, result.y)
        self.assertEqual(10, result.m)
        self.assertEqual(1, result.d)

    def test_invalid_chars(self):
        first = DateWrapper(iso_string="2022-??-10")
        self.assertEqual(2022, first.y)
        self.assertEqual(None, first.m)
        self.assertEqual(10, first.d)
        second = DateWrapper(iso_string="2022-10-??")
        self.assertEqual(2022, second.y)
        self.assertEqual(10, second.m)
        self.assertEqual(None, second.d)
        self.assertTrue(second < first)

    # Force year to be within range 1 - 9999
    def test_year_zero(self):
        result = DateWrapper(0, 12, 10)
        self.assertEqual(1, result.y)
        self.assertEqual(12, result.m)
        self.assertEqual(10, result.d)

    def test_year_10000(self):
        result = DateWrapper(10000, 12, 10)
        self.assertEqual(9999, result.y)
        self.assertEqual(12, result.m)
        self.assertEqual(10, result.d)

    def test_less_than_year(self):
        first_date = DateWrapper(2021, 12, 10)
        second_date = DateWrapper(2022, 12, 10)
        self.assertTrue(first_date < second_date)

    def test_less_than_month(self):
        first_date = DateWrapper(2022, 11, 10)
        second_date = DateWrapper(2022, 12, 10)
        self.assertTrue(first_date < second_date)

    def test_less_than_day(self):
        first_date = DateWrapper(2022, 12, 9)
        second_date = DateWrapper(2022, 12, 10)
        self.assertTrue(first_date < second_date)

    # If a value is None, that date should be bigger
    # This means when testing for oldest (smallest) the one with values gets picked
    def test_less_than_none_month(self):
        first_date = DateWrapper(2022, None, 9)
        second_date = DateWrapper(2022, 12, 10)
        self.assertFalse(first_date < second_date)

    def test_less_than_none_day(self):
        first_date = DateWrapper(2022, 12, None)
        second_date = DateWrapper(2022, 12, 10)
        self.assertFalse(first_date < second_date)

    def test_less_than_none_month_day(self):
        first_date = DateWrapper(2022, None, None)
        second_date = DateWrapper(2022, 1, 1)
        self.assertFalse(first_date < second_date)

    def test_less_than_none_month_backwards(self):
        first_date = DateWrapper(2022, 12, 9)
        second_date = DateWrapper(2022, None, 10)
        self.assertTrue(first_date < second_date)

    def test_less_than_none_day_backwards(self):
        first_date = DateWrapper(2022, 12, 10)
        second_date = DateWrapper(2022, 12, None)
        self.assertTrue(first_date < second_date)

    def test_less_than_none_month_day_backwards(self):
        first_date = DateWrapper(2022, 1, 1)
        second_date = DateWrapper(2022, None, None)
        self.assertTrue(first_date < second_date)

    def test_equal(self):
        first_date = DateWrapper(2022, 12, 10)
        second_date = DateWrapper(2022, 12, 10)
        self.assertEqual(first_date, first_date)
        self.assertEqual(first_date, second_date)

    def test_equal_none_month(self):
        first_date = DateWrapper(2022, None, 10)
        second_date = DateWrapper(2022, 12, 10)
        self.assertNotEqual(first_date, second_date)

    def test_equal_none_month_backwards(self):
        first_date = DateWrapper(2022, 12, 10)
        second_date = DateWrapper(2022, None, 10)
        self.assertNotEqual(first_date, second_date)

    def test_equal_none_months(self):
        first_date = DateWrapper(2022, None, 10)
        second_date = DateWrapper(2022, None, 10)
        self.assertTrue(first_date == second_date)

    def test_equal_none_day(self):
        first_date = DateWrapper(2022, 12, None)
        second_date = DateWrapper(2022, 12, 10)
        self.assertNotEqual(first_date, second_date)

    def test_equal_none_day_backwards(self):
        first_date = DateWrapper(2022, 12, 10)
        second_date = DateWrapper(2022, 12, None)
        self.assertNotEqual(first_date, second_date)

    def test_equal_none_days(self):
        first_date = DateWrapper(2022, 12, None)
        second_date = DateWrapper(2022, 12, None)
        self.assertTrue(first_date == second_date)

    def test_isostring(self):
        first_date = DateWrapper(iso_string="2022-12-10")
        second_date = DateWrapper(2022, 12, 10)
        self.assertTrue(first_date == second_date)

    def test_isostring_year_month(self):
        first_date = DateWrapper(iso_string="2022-12")
        second_date = DateWrapper(2022, 12)
        self.assertTrue(first_date == second_date)

    def test_isostring_year(self):
        first_date = DateWrapper(iso_string="2022")
        second_date = DateWrapper(2022)
        self.assertTrue(first_date == second_date)

    def test_isostring_empty(self):
        with self.assertRaises(ValueError):
            DateWrapper(iso_string="")

    def test_no_year_no_isostring(self):
        with self.assertRaises(TypeError):
            DateWrapper()

    def test_today(self):
        first_date = DateWrapper.today()
        today = datetime.datetime.today()
        second_date = DateWrapper(today.year, today.month, today.day)

        self.assertEqual(first_date, second_date)


class ParseMusicBrainzDateTest(unittest.TestCase):
    def parts(self, value):
        date = parse_musicbrainz_date(value)
        return None if date is None else (date.y, date.m, date.d)

    def test_full_and_partial_dates(self):
        self.assertEqual((1982, 11, 30), self.parts('1982-11-30'))
        self.assertEqual((1982, 11, None), self.parts('1982-11'))
        self.assertEqual((1982, None, None), self.parts('1982'))

    def test_unknown_components_not_invented(self):
        self.assertEqual((2015, None, None), self.parts('2015-??-??'))
        self.assertEqual((2015, 7, None), self.parts('2015-07-??'))
        self.assertEqual((2015, None, None), self.parts('2015-??-13'))  # Day without month is unusable
        self.assertEqual((2015, None, None), self.parts('2015-00-00'))
        self.assertEqual((2015, 7, None), self.parts('2015-07-00'))
        self.assertEqual((2015, None, None), self.parts('2015-??'))

    def test_invalid(self):
        for value in (None, '', '0000', '15', 'unknown', '2015-13-01', '2015-02-30', '2015/07/01'):
            self.assertIsNone(parse_musicbrainz_date(value), value)

    def test_comparison_of_partial_dates(self):
        self.assertTrue(parse_musicbrainz_date('2015-07-??') < parse_musicbrainz_date('2015-??-??'))
        self.assertTrue(parse_musicbrainz_date('2014-??-??') < parse_musicbrainz_date('2015-01-01'))
