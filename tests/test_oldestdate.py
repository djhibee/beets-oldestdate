import unittest
from unittest import mock
from unittest.mock import patch

from beets.library import Item

from beetsplug import oldestdate
from beetsplug.date_wrapper import DateWrapper


class OldestDatePluginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oldestdateplugin = oldestdate.OldestDatePlugin()

    def setUp(self):
        self.recording_id = 20
        self.recording = {"recording": {"id": self.recording_id}, "begin": "1978", "releases": [{"date": "1977"}]}
        self.recordings = [self.recording]
        self.is_cover = False
        self.approach = "recordings"

    # Test recordings approach

    def test_get_work_id_from_recording(self):
        test_recording = {"relations": [{"target-type": "work", "work": {"id": "20"}}]}
        result = self.oldestdateplugin._get_work_id_from_recording(test_recording)
        self.assertEqual("20", result)

    def test_extract_oldest_recording_date(self):
        recordings = [{"recording": {"id": 20}, "begin": "2020-12-12"}]
        starting_date = DateWrapper(iso_string="20221010")
        expected_date = DateWrapper(iso_string="20201212")
        result = self.oldestdateplugin._extract_oldest_recording_date(recordings, starting_date, self.is_cover,
                                                                      self.approach)
        self.assertEqual(expected_date, result)

    def test_extract_oldest_recording_date_with_only_year(self):
        recordings = [{"recording": {"id": 20}, "begin": "1978"}]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1978)
        result = self.oldestdateplugin._extract_oldest_recording_date(recordings, starting_date, self.is_cover,
                                                                      self.approach)
        self.assertEqual(expected_date, result)

    def test_extract_oldest_recording_date_cover(self):
        recordings = [{"recording": {"id": 20}, "begin": "1978", "attributes": ["cover"]},
                      {"recording": {"id": 20}, "begin": "1976"}]  # non-cover should be filtered out
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1978)
        result = self.oldestdateplugin._extract_oldest_recording_date(recordings, starting_date, True, self.approach)
        self.assertEqual(expected_date, result)

    def test_extract_oldest_recording_date_non_cover(self):
        # cover should be filtered out
        recordings = [{"recording": {"id": 20}, "begin": "1976", "attributes": ["cover"]},
                      {"recording": {"id": 20}, "begin": "1978"}]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1978)
        result = self.oldestdateplugin._extract_oldest_recording_date(recordings, starting_date, False, self.approach)
        self.assertEqual(expected_date, result)

    # Test releases approach

    def test_extract_oldest_release_date(self):
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1977)
        # Put recording into cache to avoid calling the API
        self.oldestdateplugin._recordings_cache[self.recording_id] = self.recording
        result = self.oldestdateplugin._extract_oldest_release_date([self.recording], starting_date, self.is_cover,
                                                                    "releases")
        self.assertEqual(expected_date, result)

    def test_extract_oldest_release_date_cover(self):
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "1978", "releases": [{"date": "1976"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "1978", "attributes": ["cover"],
             "releases": [{"date": "1977"}], "artist-credit": [{"artist": {"id": "artist-id"}}]}
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1977)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._extract_oldest_release_date(recordings, starting_date, True, ["artist-id"])
        self.assertEqual(expected_date, result)

    def test_extract_oldest_release_date_non_cover(self):
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "1978", "releases": [{"date": "1976"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "1978", "attributes": ["cover"],
             "releases": [{"date": "1977"}], "artist-credit": [{"artist": {"id": "artist-id"}}]},
            {"recording": {"id": self.recording_id + 2}, "begin": "1978", "attributes": ["cover"],
             "releases": [{"date": "1975"}], "artist-credit": [{"artist": {"id": "another-id"}}]}
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1976)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._extract_oldest_release_date(recordings, starting_date, False, ["artist-id"])
        self.assertEqual(expected_date, result)

    def test_extract_oldest_release_date_filter_recordings(self):
        self.oldestdateplugin.config['filter_recordings'] = True
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "1978", "releases": [{"date": "1976"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "1978", "attributes": ["live"],
             "releases": [{"date": "1977"}], "artist-credit": [{"artist": {"id": "artist-id"}}]},
            {"recording": {"id": self.recording_id + 2}, "begin": "1978", "attributes": ["cover"],
             "releases": [{"date": "1975"}], "artist-credit": [{"artist": {"id": "another-id"}}]}
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1976)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._extract_oldest_release_date(recordings, starting_date, False, ["artist-id"])
        self.assertEqual(expected_date, result)
        self.oldestdateplugin.config['filter_recordings'] = False

    def test_extract_oldest_release_date_release_type(self):
        self.oldestdateplugin.config['release_types'] = ["Official"]
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "1978",
             "releases": [{"date": "1976", "status": "Bootleg"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "1978", "attributes": ["live"],
             "releases": [{"date": "1977", "status": "Official"}]},
            {"recording": {"id": self.recording_id + 2}, "begin": "1978", "attributes": ["cover"],
             "releases": [{"date": "1975"}], "artist-credit": [{"artist": {"id": "another-id"}}]}
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1977)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._extract_oldest_release_date(recordings, starting_date, False, ["artist-id"])
        self.assertEqual(expected_date, result)
        self.oldestdateplugin.config['release_types'] = None

    def test_iterate_dates_recordings(self):
        self.oldestdateplugin.config['approach'] = "recordings"
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "1978",
             "releases": [{"date": "1976", "status": "Bootleg"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "1978", "attributes": ["live"],
             "releases": [{"date": "1977", "status": "Official"}]},
            {"recording": {"id": self.recording_id + 2}, "begin": "1978", "attributes": ["cover"],
             "releases": [{"date": "1975"}]}
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1978)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._iterate_dates(recordings, starting_date, False, [])
        self.assertEqual(expected_date, result)
        self.oldestdateplugin.config['approach'] = "releases"

    def test_iterate_dates_releases(self):
        self.oldestdateplugin.config['approach'] = "releases"
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "1978",
             "releases": [{"date": "1976", "status": "Bootleg"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "1978", "attributes": ["live"],
             "releases": [{"date": "1975", "status": "Official"}]},
            {"recording": {"id": self.recording_id + 2}, "begin": "1974", "attributes": ["cover"],
             "releases": [{"date": "1975"}]}  # cover gets filtered out
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1975)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._iterate_dates(recordings, starting_date, False, [])
        self.assertEqual(expected_date, result)

    def test_iterate_dates_hybrid_found(self):
        self.oldestdateplugin.config['approach'] = "hybrid"
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "1978",
             "releases": [{"date": "1976", "status": "Bootleg"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "1978", "attributes": ["live"],
             "releases": [{"date": "1975", "status": "Official"}]},
            {"recording": {"id": self.recording_id + 2}, "begin": "1974", "attributes": ["cover"],
             "releases": [{"date": "1975"}]}  # cover gets filtered out
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1978)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._iterate_dates(recordings, starting_date, False, [])
        self.assertEqual(expected_date, result)
        self.oldestdateplugin.config['approach'] = "releases"

    def test_iterate_dates_hybrid_not_found(self):
        self.oldestdateplugin.config['approach'] = "hybrid"
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "",
             "releases": [{"date": "1976", "status": "Bootleg"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "", "attributes": ["live"],
             "releases": [{"date": "1975", "status": "Official"}]},
            {"recording": {"id": self.recording_id + 2}, "begin": "", "attributes": ["cover"],
             "releases": [{"date": "1975"}]}  # cover gets filtered out
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1975)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._iterate_dates(recordings, starting_date, False, [])
        self.assertEqual(expected_date, result)
        self.oldestdateplugin.config['approach'] = "releases"

    def test_iterate_dates_both(self):
        self.oldestdateplugin.config['approach'] = "both"
        recordings = [
            {"recording": {"id": self.recording_id}, "begin": "",
             "releases": [{"date": "1976", "status": "Bootleg"}]},
            {"recording": {"id": self.recording_id + 1}, "begin": "1974", "attributes": ["live"],
             "releases": [{"date": "1975", "status": "Official"}]},
            {"recording": {"id": self.recording_id + 2}, "begin": "1973", "attributes": ["cover"],
             "releases": [{"date": "1975"}]}  # cover gets filtered out
        ]
        starting_date = DateWrapper(2022, 10, 10)
        expected_date = DateWrapper(1974)

        # Put recordings into cache to avoid calling the API
        for i in range(len(recordings)):
            self.oldestdateplugin._recordings_cache[self.recording_id + i] = recordings[i]

        result = self.oldestdateplugin._iterate_dates(recordings, starting_date, False, [])
        self.assertEqual(expected_date, result)
        self.oldestdateplugin.config['approach'] = "releases"

    # Test data_source not being Musicbrainz
    def test_track_distance_skip_non_musicbrainz_source(self):
        self.oldestdateplugin.config['filter_on_import'] = True
        # Create a mock track info with a non-MusicBrainz source
        mock_info = mock.Mock()
        mock_info.data_source = "NonMusicBrainz"

        # Make sure track does not have work id
        with patch.object(self.oldestdateplugin, '_has_work_id', return_value=False):
            dist = self.oldestdateplugin.track_distance(None, mock_info)

        # Assert that the distance is zero, indicating that the track was skipped
        self.assertEqual(0, dist.distance)

    def test_track_distance_dont_skip_musicbrainz_source(self):
        self.oldestdateplugin.config['filter_on_import'] = True
        # Create a mock track info with a MusicBrainz source
        mock_info = mock.Mock()
        mock_info.data_source = "MusicBrainz"

        # Make sure track does not have work id
        with patch.object(self.oldestdateplugin, '_has_work_id', return_value=False):
            dist = self.oldestdateplugin.track_distance(None, mock_info)

        # Assert that the distance is not zero, indicating that the track was used
        self.assertEqual(1, dist.distance)

    @patch('logging.Logger.info')
    def test_process_file_musicbrainz(self, mock_log):
        # Create a mock item with a MusicBrainz track ID and source
        self.oldestdateplugin.config['force'] = False
        item = Item(mb_trackid="some_track_id", data_source="MusicBrainz", artist="Test Artist", title="Test Title",
                    recording_year="2022")

        # Call _process_file method
        self.oldestdateplugin._process_file(item)

        # Assert that the log method was not called, indicating that the track was not skipped
        mock_log.info.assert_not_called()

    @patch('logging.Logger.info')
    def test_process_file_non_musicbrainz(self, mock_log):
        # Create a mock item with a non MusicBrainz track ID and source
        self.oldestdateplugin.config['force'] = False
        item = Item(mb_trackid="some_track_id", data_source="NonMusicBrainz", artist="Test Artist", title="Test Title")

        # Call _process_file method
        self.oldestdateplugin._process_file(item)

        # Assert that the log method was called, indicating that the track was skipped
        mock_log.assert_called_once_with(
            'Skipping track with no mb_trackid: {0.artist} - {0.title}', item
        )


if __name__ == '__main__':
    unittest.main()
