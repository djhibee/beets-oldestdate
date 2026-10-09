import os
import unittest
from unittest import mock
from unittest.mock import patch

from beets.autotag import AlbumInfo, TrackInfo
from beets.importer import action
from beets.library import Item, Library

from beetsplug import oldestdate
from beetsplug.date_wrapper import DateWrapper
from tests import thriller_fixtures as fx


# Plugins can only be instantiated once (media fields are registered globally)
PLUGIN = oldestdate.OldestDatePlugin()
DEFAULT_CONFIG = PLUGIN.config.flatten()


def reset_plugin():
    PLUGIN.config.set(dict(DEFAULT_CONFIG))
    PLUGIN._mb = None
    PLUGIN._importing = False
    PLUGIN._recordings_cache.clear()
    PLUGIN._releases_cache.clear()
    PLUGIN._works_cache.clear()
    return PLUGIN


class OldestDatePluginTestCase(unittest.TestCase):
    def setUp(self):
        self.plugin = reset_plugin()
        self.mb = fx.FakeMusicBrainzClient()
        self.plugin._mb = self.mb
        self.lib = Library(':memory:')
        write_patcher = patch.object(Item, 'write')  # Never touch files
        self.item_write = write_patcher.start()
        self.addCleanup(write_patcher.stop)

    def tearDown(self):
        self.lib._close()
        reset_plugin()

    def make_item(self, singleton=True, comp=False, albumtypes=None, **kwargs):
        values = dict(title='Thriller', artist='Michael Jackson', mb_trackid=fx.RECORDING_ID,
                      mb_albumid=fx.RELEASE_ID, data_source='MusicBrainz', year=2005, month=10, day=17,
                      comp=comp, albumtypes=albumtypes or [])
        values.update(kwargs)
        item = Item(**values)
        if singleton:
            self.lib.add(item)
        else:
            self.lib.add_album([item])
        return item


class ApproachSelectionTest(OldestDatePluginTestCase):
    def test_normalize_approach(self):
        self.assertEqual('release', oldestdate.normalize_approach('Releases'))
        self.assertEqual('release', oldestdate.normalize_approach('album'))
        self.assertEqual('recording', oldestdate.normalize_approach('singleton'))
        self.assertEqual('work', oldestdate.normalize_approach('work'))
        with self.assertRaises(ValueError):
            oldestdate.normalize_approach('hybrid')

    def test_defaults(self):
        self.assertEqual('recording', self.plugin._get_approach(self.make_item(singleton=True)))
        self.assertEqual('release', self.plugin._get_approach(self.make_item(singleton=False)))
        self.assertEqual('recording', self.plugin._get_approach(self.make_item(singleton=False, comp=True)))

    def test_configured(self):
        self.plugin.config['singleton'] = 'work'
        self.plugin.config['album'] = 'recording'
        self.plugin.config['compilation'] = 'release'
        self.assertEqual('work', self.plugin._get_approach(self.make_item(singleton=True)))
        self.assertEqual('recording', self.plugin._get_approach(self.make_item(singleton=False)))
        self.assertEqual('release', self.plugin._get_approach(self.make_item(singleton=False, comp=True)))

    def test_album_type_has_priority(self):
        self.plugin.config['album_type'] = [['soundtrack', 'album'], ['live', 'work']]
        item = self.make_item(singleton=False, comp=True, albumtypes=['album', 'soundtrack'])
        self.assertEqual('release', self.plugin._get_approach(item))
        item = self.make_item(singleton=False, albumtypes=['album', 'live'])
        self.assertEqual('work', self.plugin._get_approach(item))
        item = self.make_item(singleton=False, albumtypes=['album'])
        self.assertEqual('release', self.plugin._get_approach(item))

    def test_album_type_mapping_and_albumtype_field(self):
        self.plugin.config['album_type'] = {'Soundtrack': 'work'}
        item = self.make_item(singleton=False, albumtype='soundtrack')
        self.assertEqual('work', self.plugin._get_approach(item))

    def test_album_type_ignored_for_singletons(self):
        self.plugin.config['album_type'] = {'soundtrack': 'work'}
        item = self.make_item(singleton=True, albumtypes=['soundtrack'])
        self.assertEqual('recording', self.plugin._get_approach(item))

    def test_invalid_approach(self):
        self.plugin.config['album'] = 'hybrid'
        with self.assertRaises(ValueError):
            self.plugin._get_approach(self.make_item(singleton=False))


class ThrillerApproachesTest(OldestDatePluginTestCase):
    """Recording https://musicbrainz.org/recording/2eec3a3b-33af-4ea1-b169-7450b941732d"""

    def test_recording_approach(self):
        self.assertEqual(DateWrapper(2005), self.plugin._get_oldest_date(self.make_item(), 'recording'))

    def test_release_approach(self):
        date = self.plugin._get_oldest_date(self.make_item(singleton=False), 'release')
        self.assertEqual(DateWrapper(2005), date)
        self.assertEqual([('release', fx.RELEASE_ID, ('release-groups',))], self.mb.calls)

    def test_release_approach_without_album_id(self):
        self.assertIsNone(self.plugin._get_oldest_date(self.make_item(mb_albumid=''), 'release'))

    def test_work_approach(self):
        date = self.plugin._get_oldest_date(self.make_item(), 'work')
        self.assertEqual(DateWrapper(1982, 11, 30), date)
        # Dates come from the work's recording relations, no extra recording lookups needed
        self.assertEqual(['recording', 'work'], [call[0] for call in self.mb.calls])

    def test_work_approach_without_filter_recordings(self):
        self.plugin.config['filter_recordings'] = False
        date = self.plugin._get_oldest_date(self.make_item(), 'work')
        self.assertEqual(DateWrapper(1981), date)  # Live recording is kept, cover is still removed

    def test_work_approach_cover_keeps_only_covers(self):
        # For a cover, the original (non-cover) recordings are ignored
        date = self.plugin._get_oldest_date(self.make_item(mb_trackid=fx.COVER_RECORDING_ID), 'work')
        self.assertEqual(DateWrapper(1980), date)
        self.assertNotIn(('recording', fx.ORIGINAL_RECORDING_ID, ('artists', 'work-rels')), self.mb.calls)

    def test_work_approach_cover_filters_other_artists(self):
        # Turn the 1982 recording into a cover by another artist: it must be ignored for a cover track
        recording = self.mb.get_recording(fx.ORIGINAL_RECORDING_ID)
        recording['artist-credit'][0]['artist']['id'] = 'another-artist'
        self.plugin._recordings_cache[fx.ORIGINAL_RECORDING_ID] = recording
        work = self.mb.get_work(fx.WORK_ID)
        for rel in work['relations']:
            rel['attributes'] = ['cover']
        self.plugin._works_cache[fx.WORK_ID] = work
        recording = self.mb.get_recording(fx.RECORDING_ID)
        recording['relations'][0]['attributes'] = ['cover']
        self.plugin._recordings_cache[fx.RECORDING_ID] = recording

        date = self.plugin._get_oldest_date(self.make_item(), 'work')
        self.assertEqual(DateWrapper(1981), date)  # Live recording by the same artist, not the other artist

    def test_work_approach_release_types(self):
        self.plugin.config['release_types'] = ['Official']
        self.plugin.config['filter_recordings'] = False
        date = self.plugin._get_oldest_date(self.make_item(), 'work')
        self.assertEqual(DateWrapper(1982, 11, 30), date)  # 1981 live recording is a bootleg

    def test_work_approach_without_work(self):
        recording = self.mb.get_recording(fx.RECORDING_ID)
        recording['relations'] = []
        self.plugin._recordings_cache[fx.RECORDING_ID] = recording
        self.assertEqual(DateWrapper(2005), self.plugin._get_oldest_date(self.make_item(), 'work'))

    def test_use_file_date(self):
        self.plugin.config['use_file_date'] = True
        item = self.make_item(year=1999, month=0, day=0)
        self.assertEqual(DateWrapper(1999), self.plugin._get_oldest_date(item, 'recording'))

    def test_process_file_per_approach(self):
        self.plugin.config['album_type'] = {'live': 'work'}
        singleton = self.make_item(singleton=True)
        album_item = self.make_item(singleton=False)
        work_item = self.make_item(singleton=False, albumtypes=['album', 'live'])
        for item in (singleton, album_item, work_item):
            self.plugin._process_file(item)
        self.assertEqual(2005, singleton.recording_year)
        self.assertEqual(2005, album_item.recording_year)
        self.assertEqual(1982, work_item.recording_year)
        self.assertEqual(11, work_item.recording_month)
        self.assertEqual(30, work_item.recording_day)

    def test_process_file_network_error(self):
        item = self.make_item(mb_trackid='unknown')
        self.plugin._process_file(item)
        self.assertNotIn('recording_year', item)


class ImportTest(OldestDatePluginTestCase):
    def test_get_work_id_from_recording(self):
        test_recording = {"relations": [{"target-type": "work", "work": {"id": "20"}}]}
        self.assertEqual("20", self.plugin._get_work_id_from_recording(test_recording))

    # Test data_source not being Musicbrainz
    def test_track_distance_skip_non_musicbrainz_source(self):
        self.plugin.config['filter_on_import'] = True
        mock_info = mock.Mock()
        mock_info.data_source = "NonMusicBrainz"

        # Make sure track does not have work id
        with patch.object(self.plugin, '_has_work_id', return_value=False):
            dist = self.plugin.track_distance(None, mock_info)

        # Assert that the distance is zero, indicating that the track was skipped
        self.assertEqual(0, dist.distance)

    def test_track_distance_dont_skip_musicbrainz_source(self):
        self.plugin.config['filter_on_import'] = True
        mock_info = mock.Mock()
        mock_info.data_source = "MusicBrainz"

        # Make sure track does not have work id
        with patch.object(self.plugin, '_has_work_id', return_value=False):
            dist = self.plugin.track_distance(None, mock_info)

        # Assert that the distance is not zero, indicating that the track was used
        self.assertEqual(1, dist.distance)

    @patch('logging.Logger.info')
    def test_process_file_already_processed(self, mock_log):
        self.plugin.config['force'] = False
        item = Item(mb_trackid="some_track_id", data_source="MusicBrainz", artist="Test Artist", title="Test Title",
                    recording_year="2022")
        self.plugin._process_file(item)
        mock_log.assert_called_once_with('Skipping already processed track: {0.artist} - {0.title}', item)
        self.assertEqual([], self.mb.calls)

    @patch('logging.Logger.info')
    def test_process_file_non_musicbrainz(self, mock_log):
        self.plugin.config['force'] = False
        item = Item(mb_trackid="some_track_id", data_source="NonMusicBrainz", artist="Test Artist", title="Test Title")
        self.plugin._process_file(item)
        mock_log.assert_called_once_with('Skipping track with no mb_trackid: {0.artist} - {0.title}', item)


class OptionsTest(OldestDatePluginTestCase):
    def test_overwrite_date_bool(self):
        self.plugin.config['overwrite_date'] = True
        item = self.make_item(singleton=True)
        self.plugin.config['singleton'] = 'work'
        self.plugin._process_file(item)
        self.assertEqual((1982, 11, 30), (item.year, item.month, item.day))

    def test_overwrite_date_only_for_release_approach(self):
        self.plugin.config['overwrite_date'] = ['release']
        self.plugin.config['singleton'] = 'work'
        singleton = self.make_item(singleton=True)
        self.plugin._process_file(singleton)
        self.assertEqual(1982, singleton.recording_year)
        self.assertEqual((2005, 10, 17), (singleton.year, singleton.month, singleton.day))  # Untouched

        album_item = self.make_item(singleton=False, year=2010)
        self.plugin._process_file(album_item)
        self.assertEqual(2005, album_item.year)  # Overwritten

    def test_overwrite_date_single_approach_string(self):
        self.plugin.config['overwrite_date'] = 'recording'
        self.assertTrue(self.plugin._overwrite_date('recording'))
        self.assertFalse(self.plugin._overwrite_date('work'))

    @patch('beets.ui.input_options', return_value='n')
    def test_prompt_for_no_skips_processing(self, input_options):
        self.plugin.config['prompt_for'] = ['work']
        self.plugin.config['singleton'] = 'work'
        item = self.make_item(singleton=True)
        self.plugin._process_file(item)
        input_options.assert_called_once_with(('Yes', 'No'))
        self.assertNotIn('recording_year', item)
        self.assertEqual([], self.mb.calls)  # Asked before any lookup

    @patch('beets.ui.input_options', return_value='y')
    def test_prompt_for_yes_processes(self, input_options):
        self.plugin.config['prompt_for'] = ['recording', 'work']
        self.plugin.config['singleton'] = 'work'
        item = self.make_item(singleton=True)
        self.plugin._process_file(item)
        input_options.assert_called_once()
        self.assertEqual(1982, item.recording_year)

    @patch('beets.ui.input_options')
    def test_prompt_for_other_approach(self, input_options):
        self.plugin.config['prompt_for'] = ['work']
        item = self.make_item(singleton=True)  # recording approach
        self.plugin._process_file(item)
        input_options.assert_not_called()
        self.assertEqual(2005, item.recording_year)

    @patch('beets.ui.input_options')
    def test_prompt_for_not_asked_for_already_processed(self, input_options):
        self.plugin.config['prompt_for'] = ['recording']
        item = self.make_item(singleton=True, recording_year=2000)
        self.plugin._process_file(item)
        input_options.assert_not_called()

    @patch('beets.ui.input_options', return_value='n')
    def test_prompt_for_compilation_with_singleton_alias(self, input_options):
        # Compilation processed with the recording ("singleton") approach, which requires a prompt
        self.plugin.config['compilation'] = 'singleton'
        self.plugin.config['prompt_for'] = ['singleton']
        item = self.make_item(singleton=False, comp=True)
        self.plugin._process_file(item)
        input_options.assert_called_once()
        self.assertNotIn('recording_year', item)

    def album_items(self):
        items = [Item(title='Thriller', artist='Michael Jackson', album='Thriller', mb_trackid=fx.RECORDING_ID,
                      mb_albumid=fx.RELEASE_ID, data_source='MusicBrainz', track=i) for i in (1, 2)]
        self.lib.add_album(items)
        return items

    @patch('beets.ui.input_options', return_value='y')
    def test_prompt_once_per_album(self, input_options):
        self.plugin.config['prompt_for'] = ['release']
        self.album_items()
        self.plugin._command_func(self.lib, mock.Mock(approach=None, force=None), [])
        input_options.assert_called_once()
        self.assertEqual([2005, 2005], [int(item.recording_year) for item in self.lib.items()])

    @patch('beets.ui.input_options', return_value='n')
    def test_prompt_album_on_import(self, input_options):
        self.plugin.config['prompt_for'] = ['release']
        items = self.album_items()
        task = mock.Mock()
        task.imported_items.return_value = items
        self.plugin._on_import(None, task)
        input_options.assert_called_once()
        self.assertTrue(all('recording_year' not in item for item in items))

    @patch('beets.ui.input_options')
    def test_prompt_for_quiet_import(self, input_options):
        from beets import config
        self.plugin.config['prompt_for'] = ['recording']
        config['import']['quiet'] = True
        try:
            item = self.make_item(singleton=True)
            self.plugin._process_file(item)
        finally:
            config['import']['quiet'] = False
        input_options.assert_not_called()
        self.assertEqual(2005, item.recording_year)

    def test_command_forced_approach(self):
        item = self.make_item(singleton=True)
        self.plugin._command_func(self.lib, mock.Mock(approach='work', force=None), [])
        item.load()
        self.assertEqual(1982, int(item.recording_year))
        self.item_write.assert_called_once()

        # Already processed: skipped unless forced
        self.plugin._command_func(self.lib, mock.Mock(approach='recording', force=None), [])
        item.load()
        self.assertEqual(1982, int(item.recording_year))
        self.plugin._command_func(self.lib, mock.Mock(approach='recording', force=True), [])
        item.load()
        self.assertEqual(2005, int(item.recording_year))

    def test_command_invalid_approach(self):
        from beets.ui import UserError
        with self.assertRaises(UserError):
            self.plugin._command_func(self.lib, mock.Mock(approach='hybrid', force=None), [])

    def test_command_parser(self):
        command = self.plugin.commands()[0]
        opts, _ = command.parser.parse_args(['-a', 'work', '-f'])
        self.assertEqual('work', opts.approach)
        self.assertTrue(opts.force)


class MissingWorkIdTest(OldestDatePluginTestCase):
    def setUp(self):
        super().setUp()
        recording = self.mb.get_recording(fx.RECORDING_ID)
        recording['relations'] = []  # No work
        self.plugin._recordings_cache[fx.RECORDING_ID] = recording
        self.track = TrackInfo(title='Thriller', artist='Michael Jackson', track_id=fx.RECORDING_ID,
                               data_source='MusicBrainz')

    def singleton_task(self):
        return mock.Mock(is_album=False, match=mock.Mock(info=self.track), choice_flag=None)

    def album_task(self, **album_info):
        info = AlbumInfo(tracks=[self.track], **album_info)
        return mock.Mock(is_album=True, match=mock.Mock(info=info, mapping={'item': self.track}), choice_flag=None)

    @patch('beets.ui.input_options')
    def test_not_prompted_for_recording_approach(self, input_options):
        task = self.singleton_task()
        self.plugin._import_task_choice(task, None)
        input_options.assert_not_called()
        self.assertIsNone(task.choice_flag)

    @patch('beets.ui.input_options', return_value='s')
    def test_prompted_for_work_approach(self, input_options):
        self.plugin.config['singleton'] = 'work'
        task = self.singleton_task()
        self.plugin._import_task_choice(task, None)
        input_options.assert_called_once_with(('Use this recording', 'Try again', 'Skip track'))
        self.assertEqual(action.SKIP, task.choice_flag)

    @patch('beets.ui.input_options', return_value='u')
    def test_use_recording(self, input_options):
        self.plugin.config['singleton'] = 'work'
        task = self.singleton_task()
        self.plugin._import_task_choice(task, None)
        self.assertIsNone(task.choice_flag)

    @patch('beets.ui.input_options')
    def test_album_not_prompted_for_release_approach(self, input_options):
        self.plugin._import_task_choice(self.album_task(), None)
        input_options.assert_not_called()

    @patch('beets.ui.input_options', return_value='s')
    def test_album_prompted_for_work_album_type(self, input_options):
        self.plugin.config['album_type'] = {'live': 'work'}
        task = self.album_task(albumtypes=['album', 'live'])
        self.plugin._import_task_choice(task, None)
        input_options.assert_called_once_with(('Use this recording', 'Try again', 'Skip album'))
        self.assertEqual(action.SKIP, task.choice_flag)

    @patch('beets.ui.input_options', return_value='s')
    def test_compilation_prompted_for_work(self, input_options):
        self.plugin.config['compilation'] = 'work'
        self.plugin._import_task_choice(self.album_task(va=True), None)
        input_options.assert_called_once()


@unittest.skipUnless(os.environ.get('OLDESTDATE_ONLINE_TESTS'), 'Set OLDESTDATE_ONLINE_TESTS=1 to query MusicBrainz')
class ThrillerOnlineTest(unittest.TestCase):
    """Same scenarios as ThrillerApproachesTest, against the live MusicBrainz API"""

    @classmethod
    def setUpClass(cls):
        cls.plugin = reset_plugin()
        recording = cls.plugin.mb.get_recording(fx.RECORDING_ID, includes=['releases'])
        releases = [r for r in recording.get('releases', []) if r.get('date')]
        cls.release_id = min(releases, key=lambda r: r['date'])['id']

    def item(self):
        return Item(mb_trackid=fx.RECORDING_ID, mb_albumid=self.release_id, data_source='MusicBrainz')

    def test_recording(self):
        self.assertEqual(2005, self.plugin._get_oldest_date(self.item(), 'recording').y)

    def test_release(self):
        self.assertEqual(2005, self.plugin._get_oldest_date(self.item(), 'release').y)

    def test_work(self):
        self.assertEqual(1982, self.plugin._get_oldest_date(self.item(), 'work').y)


if __name__ == '__main__':
    unittest.main()
