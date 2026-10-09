from typing import Optional, Any, List, Dict, Iterable, Tuple, Union
from urllib.parse import quote_plus

import mediafile
from beets import ui, config
from beets.autotag import hooks, TrackInfo
from beets.importer import action, ImportTask, ImportSession
from beets.library import Item, Library
from beets.plugins import BeetsPlugin

from . import mb_api
from .date_wrapper import DateWrapper

# Type alias
Recording = Dict[str, Any]
Release = Dict[str, Any]
Work = Dict[str, Any]

# Approaches used to find the oldest date of a track:
# - release: first release date of the release group of the item's album (mb_albumid)
# - recording: first release date of the item's recording (mb_trackid)
# - work: oldest first release date of all recordings of the work associated with the item's recording
RELEASE = 'release'
RECORDING = 'recording'
WORK = 'work'
APPROACHES = (RELEASE, RECORDING, WORK)
APPROACH_ALIASES = {
    'releases': RELEASE, 'album': RELEASE,
    'recordings': RECORDING, 'singleton': RECORDING,
    'works': WORK,
}


def format_date(date: DateWrapper) -> str:
    """Format date as YYYY, YYYY-MM or YYYY-MM-DD depending on known fields"""
    result = str(date.y).zfill(4)
    if date.m is not None:
        result += '-' + str(date.m).zfill(2)
        if date.d is not None:
            result += '-' + str(date.d).zfill(2)
    return result


def normalize_approach(value: Any) -> str:
    """Return canonical approach name, raising ValueError if invalid"""
    approach = str(value).strip().lower()
    approach = APPROACH_ALIASES.get(approach, approach)
    if approach not in APPROACHES:
        raise ValueError('Invalid approach "{}", must be one of: {}'.format(value, ', '.join(APPROACHES)))
    return approach


class OldestDatePlugin(BeetsPlugin):  # type: ignore
    _importing: bool = False

    def __init__(self) -> None:
        super(OldestDatePlugin, self).__init__()
        self.import_stages = [self._on_import]
        self.config.add({
            'auto': True,  # Run during import phase
            'ignore_track_id': False,  # During import, ignore existing track_id
            'filter_on_import': True,  # During import, weight down candidates with no work_id
            'prompt_missing_work_id': True,  # During import, prompt to fix work_id if missing
            'force': False,  # Run even if already processed
            'overwrite_date': False,  # Overwrite date field in tags: yes/no, or list of approaches, e.g. [release]
            'overwrite_month': True,  # If overwriting date, also overwrite month field
            'overwrite_day': True,  # If overwriting date and month, also overwrite day
            'filter_recordings': True,  # Work approach: skip recordings with attributes (e.g. live)
            'singleton': RECORDING,  # Approach for singletons
            'album': RELEASE,  # Approach for standard albums
            'compilation': RECORDING,  # Approach for compilations
            'album_type': {},  # Approach by album type, e.g. {soundtrack: release}. Takes priority
            'prompt_for': [],  # Approaches for which to ask whether an item/album must be processed, e.g. [work]
            'release_types': None,  # Filter by release status, e.g. ['Official']
            'use_file_date': False,  # Also use file's embedded date when looking for oldest date
            'max_network_retries': 3  # Maximum amount of times a given network call will be retried
        })

        self._recordings_cache: Dict[str, Recording] = dict()
        self._releases_cache: Dict[str, Release] = dict()
        self._works_cache: Dict[str, Work] = dict()

        if self.config['auto']:
            if self.config['ignore_track_id']:
                self.register_listener('import_task_created', self._import_task_created)
            if self.config['prompt_missing_work_id']:
                self.register_listener('import_task_choice', self._import_task_choice)
            if self.config['filter_on_import']:
                self.register_listener('trackinfo_received', self._import_trackinfo)
                # Add heavy weight for missing work_id from a track
                config['match']['distance_weights'].add({'work_id': 4})

        self._mb: Optional[mb_api.MusicBrainzClient] = None

        for recording_field in (
                'recording_year',
                'recording_month',
                'recording_day'):
            field = mediafile.MediaField(
                mediafile.MP3DescStorageStyle(recording_field),
                mediafile.MP4StorageStyle('----:com.apple.iTunes:{}'.format(
                    recording_field)),
                mediafile.StorageStyle(recording_field))
            self.add_media_field(recording_field, field)

    def commands(self) -> List[ui.Subcommand]:
        recording_date_command = ui.Subcommand(
            'oldestdate',
            help="Retrieve the date of the oldest known recording or release of a track.",
            aliases=['olddate'])
        recording_date_command.parser.add_option(
            '-a', '--approach', dest='approach', default=None,
            help='force approach for all matched items: {}'.format(', '.join(APPROACHES)))
        recording_date_command.parser.add_option(
            '-f', '--force', dest='force', action='store_true', default=None,
            help='process items even if they have already been processed')
        recording_date_command.func = self._command_func
        return [recording_date_command]

    def _import_trackinfo(self, info: TrackInfo) -> None:
        """Fetch the recording associated with each candidate"""
        if 'track_id' in info:
            self._fetch_recording(info.track_id)

    def track_distance(self, _: Item, info: TrackInfo) -> hooks.Distance:
        dist = hooks.Distance()
        if info.data_source != 'MusicBrainz':
            self._log.debug('Skipping track with non MusicBrainz data source {0.artist} - {0.title}', info)
            return dist
        if self.config['filter_on_import'] and not self._has_work_id(info.track_id):
            dist.add('work_id', 1)

        return dist

    def _import_task_created(self, task: ImportTask, session: ImportSession) -> None:
        task.item.mb_trackid = None

    def _task_approach(self, task: ImportTask) -> str:
        """Approach that will be used for the items of an import task, based on the chosen match"""
        info = task.match.info
        if not task.is_album:
            return self._approach_for(True, False, [])
        return self._approach_for(False, bool(info.get('va')), self._album_types(info))

    def _import_task_choice(self, task: ImportTask, session: ImportSession) -> None:
        """Prompt to fix recordings without work, only needed when using the work approach"""
        if not task.match or not self.config['prompt_missing_work_id']:
            return
        if self._task_approach(task) != WORK:
            return

        if task.is_album:
            tracks = list(task.match.mapping.values())
            skip_option = 'Skip album'
        else:
            tracks = [task.match.info]
            skip_option = 'Skip track'

        try:
            for track in tracks:
                if track.get('data_source', 'MusicBrainz') != 'MusicBrainz' or not track.get('track_id'):
                    continue
                if not self._prompt_missing_work_id(track, skip_option):
                    task.choice_flag = action.SKIP
                    return
        except mb_api.MusicBrainzError as e:
            self._log.error('Could not check work for {0}: {1}', task, e)

    def _prompt_missing_work_id(self, track: TrackInfo, skip_option: str) -> bool:
        """Prompt until the recording has a work. Return False if the task must be skipped"""
        recording_id = track.track_id
        search_link = "https://musicbrainz.org/search?query=" + quote_plus(track.title or '') \
                      + "+artist%3A%22" + quote_plus(track.artist or '') \
                      + "%22&type=recording&limit=100&method=advanced"

        while not self._has_work_id(recording_id):
            recording_date = self._recording_date(recording_id)
            recording_year_string = None if recording_date is None else format_date(recording_date)

            self._log.error("{0.artist} - {0.title} ({1}) has no associated work! Please fix "
                            "and try again!", track,
                            recording_year_string)
            print("Search link: " + search_link)
            sel = ui.input_options(('Use this recording', 'Try again', skip_option))

            if sel == "t":  # Fetch data again
                self._fetch_recording(recording_id)
            elif sel == "u":
                return True
            else:
                return False
        return True

    # Approach selection

    def _album_type_approaches(self) -> List[Tuple[str, str]]:
        """Parse album_type option, either a mapping {type: approach} or a list of [type, approach] pairs"""
        value = self.config['album_type'].get()
        pairs: Iterable[Any]
        if not value:
            return []
        if isinstance(value, dict):
            pairs = value.items()
        elif isinstance(value, (list, tuple)):
            pairs = value
        else:
            raise ValueError('Invalid album_type option: {}'.format(value))

        result = []
        for pair in pairs:
            if isinstance(pair, dict) and len(pair) == 1:  # YAML list of single mappings: - soundtrack: release
                pair = next(iter(pair.items()))
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError('Invalid album_type entry: {}'.format(pair))
            result.append((str(pair[0]).strip().lower(), normalize_approach(pair[1])))
        return result

    @staticmethod
    def _album_types(entity: Any) -> List[str]:
        """Lowercase album types of an Item or AlbumInfo"""
        types: List[str] = []
        album_types: Union[None, str, List[str]] = entity.get('albumtypes')
        if isinstance(album_types, str):
            album_types = album_types.split(';')
        for album_type in list(album_types or []) + [entity.get('albumtype') or '']:
            album_type = album_type.strip().lower()
            if album_type and album_type not in types:
                types.append(album_type)
        return types

    def _approach_for(self, is_singleton: bool, is_compilation: bool, album_types: List[str]) -> str:
        if not is_singleton:
            for album_type, approach in self._album_type_approaches():
                if album_type in album_types:
                    return approach
        if is_singleton:
            return normalize_approach(self.config['singleton'].get())
        if is_compilation:
            return normalize_approach(self.config['compilation'].get())
        return normalize_approach(self.config['album'].get())

    def _get_approach(self, item: Item) -> str:
        """Choose approach for an item according to configuration"""
        return self._approach_for(item.album_id is None, bool(item.get('comp')), self._album_types(item))

    @property
    def mb(self) -> mb_api.MusicBrainzClient:
        """MusicBrainz JSON API client, configured from the global beets MusicBrainz settings"""
        if self._mb is None:
            mb_config = config['musicbrainz']
            host = str(mb_config['host'].get() or 'musicbrainz.org')
            https = bool(mb_config['https'].get()) if 'https' in mb_config.keys() else False
            ratelimit = int(mb_config['ratelimit'].get() or 1) if 'ratelimit' in mb_config.keys() else 1
            interval = float(mb_config['ratelimit_interval'].get() or 1.0) \
                if 'ratelimit_interval' in mb_config.keys() else 1.0
            self._mb = mb_api.MusicBrainzClient(
                host=host,
                https=https or host == 'musicbrainz.org',
                ratelimit=ratelimit,
                ratelimit_interval=interval,
                max_retries=int(self.config['max_network_retries'].get()),
                log=self._log,
            )
        return self._mb

    def _get_work_id_from_recording(self, recording: Recording) -> Optional[str]:
        """Extract first valid work_id from recording"""
        for work_rel in mb_api.work_relations(recording):
            if 'id' in work_rel['work']:
                return str(work_rel['work']['id'])
        return None

    def _contains_artist(self, recording: Recording, artist_ids: List[str]) -> bool:
        """Returns whether this recording contains at least one of the specified artists"""
        return any(artist_id in artist_ids for artist_id in mb_api.artist_ids(recording))

    def _get_artist_ids_from_recording(self, recording: Recording) -> List[str]:
        """Extract artist ids from a recording"""
        return mb_api.artist_ids(recording)

    def _is_cover(self, recording: Recording) -> bool:
        """Returns whether given fetched recording is a cover of a work"""
        return any('cover' in (rel.get('attributes') or []) for rel in mb_api.work_relations(recording))

    def _fetch_work(self, work_id: str) -> Work:
        """Fetch work, including recording relations"""
        return self.mb.get_work(work_id, includes=['recording-rels'])

    def _has_work_id(self, recording_id: str) -> bool:
        """Return whether the recording has a work id"""
        recording = self._get_recording(recording_id)
        work_id = self._get_work_id_from_recording(recording)
        return work_id is not None

    def _command_func(self, lib: Library, opts: Any, args: List[str]) -> None:
        """This queries the local database, not the files."""
        forced_approach = None
        if opts.approach:
            try:
                forced_approach = normalize_approach(opts.approach)
            except ValueError as e:
                raise ui.UserError(str(e))
        if opts.force is not None:
            self.config['force'] = opts.force

        self._importing = False
        # Group items by album, so that validation is asked once per album
        groups: Dict[Any, List[Item]] = {}
        for item in lib.items(args):
            key = ('album', item.album_id) if item.album_id is not None else ('item', item.id)
            groups.setdefault(key, []).append(item)
        for items in groups.values():
            self._process_items(items, forced_approach)

    def _on_import(self, _: ImportSession, task: ImportTask) -> None:
        if self.config['auto']:
            self._importing = True
            self._process_items(task.imported_items())

    def _process_items(self, items: Iterable[Item], forced_approach: Optional[str] = None) -> None:
        """Process a group of items (an album or a singleton).
        If the approach is listed in prompt_for, first ask whether the group must be processed."""
        by_approach: Dict[str, List[Item]] = {}
        for item in items:
            if self._should_process(item):
                by_approach.setdefault(forced_approach or self._get_approach(item), []).append(item)

        prompt_for = self._prompt_for()
        for approach, approach_items in by_approach.items():
            if approach in prompt_for and not self._confirm(approach_items, approach):
                self._log.info('Skipping {0} item(s) as requested ({1} approach)', len(approach_items), approach)
                continue
            for item in approach_items:
                oldest_date = self._find_date(item, approach)
                if oldest_date is not None:
                    self._apply_date(item, oldest_date, approach)

    def _process_file(self, item: Item, forced_approach: Optional[str] = None) -> None:
        self._process_items([item], forced_approach)

    def _prompt_for(self) -> List[str]:
        value = self.config['prompt_for'].get() or []
        if isinstance(value, str):
            value = [value]
        return [normalize_approach(approach) for approach in value]

    def _confirm(self, items: List[Item], approach: str) -> bool:
        """Ask the user whether the items (album or singleton) must be processed with given approach"""
        if config['import']['quiet'].get(bool):
            return True
        first = items[0]
        if first.album_id is not None:
            description = 'album {} - {} ({} track(s))'.format(first.albumartist or first.artist, first.album,
                                                               len(items))
        else:
            description = 'track {} - {}'.format(first.artist, first.title)
        print('oldestdate: process {} using the {} approach?'.format(description, approach))
        sel = ui.input_options(('Yes', 'No'))
        return bool(sel == 'y')

    def _should_process(self, item: Item) -> bool:
        """Whether the item can and must be processed"""
        if not item.mb_trackid or item.data_source != 'MusicBrainz':
            self._log.info('Skipping track with no mb_trackid: {0.artist} - {0.title}', item)
            return False

        # Check for the recording_year and if it exists and not empty skips the track (if force is not True)
        if 'recording_year' in item and item.recording_year and not self.config['force']:
            self._log.info('Skipping already processed track: {0.artist} - {0.title}', item)
            return False
        return True

    def _find_date(self, item: Item, approach: str) -> Optional[DateWrapper]:
        """Find oldest date of an item using given approach"""
        try:
            oldest_date = self._get_oldest_date(item, approach)
        except mb_api.MusicBrainzError as e:
            self._log.error('Could not fetch data from MusicBrainz for {0.artist} - {0.title}: {1}', item, e)
            return None
        finally:
            self._recordings_cache.clear()

        if not oldest_date:
            self._log.error('No date found for {0.artist} - {0.title} ({1} approach)', item, approach)
            return None

        self._log.info('Oldest date for {0.artist} - {0.title} ({1} approach): {2}', item, approach,
                       format_date(oldest_date))
        return oldest_date

    def _overwrite_date(self, approach: str) -> bool:
        """Whether date fields must be overwritten for given approach"""
        value = self.config['overwrite_date'].get()
        if isinstance(value, bool) or value is None:
            return bool(value)
        if isinstance(value, str):
            value = [value]
        return approach in [normalize_approach(a) for a in value]

    def _apply_date(self, item: Item, oldest_date: DateWrapper, approach: str) -> None:
        if oldest_date.y is not None:
            item['recording_year'] = oldest_date.y
        if oldest_date.m is not None:
            item['recording_month'] = oldest_date.m
        if oldest_date.d is not None:
            item['recording_day'] = oldest_date.d

        # Write over the date tag if configured as YYYYMMDD
        year_string = str(oldest_date.y).zfill(4)
        month_string = str(oldest_date.m).zfill(2)
        day_string = str(oldest_date.d).zfill(2)

        if self._overwrite_date(approach):
            self._log.warning(
                'Overwriting date field for: {0.artist} - {0.title} from {0.year}-{0.month}-{0.day} to {1}-{2}-{3}',
                item, year_string, month_string, day_string)
            item.year = "" if oldest_date.y is None else year_string
            item.month = "" if (oldest_date.m is None or not self.config['overwrite_month']) else month_string
            item.day = "" if (oldest_date.d is None or not self.config['overwrite_day']) else day_string

        self._log.info('Applying changes to {0.artist} - {0.title}', item)
        item.store()
        # Prevent changing file on disk before it reaches final destination
        if not self._importing:
            item.write()

    # MusicBrainz data

    def _fetch_recording(self, recording_id: str) -> Recording:
        """Fetch and cache recording from MusicBrainz, including artists and work relations"""
        includes = ['artists', 'work-rels']
        if self.config['release_types'].get():
            includes.append('releases')  # Needed to filter dates by release status
        recording: Recording = self.mb.get_recording(recording_id, includes=includes)

        self._recordings_cache[recording_id] = recording
        return recording

    def _get_recording(self, recording_id: str) -> Recording:
        """Get recording from cache or MusicBrainz"""
        return self._recordings_cache[
            recording_id] if recording_id in self._recordings_cache else self._fetch_recording(recording_id)

    def _get_release(self, release_id: str) -> Release:
        """Get release, including its release group, from cache or MusicBrainz"""
        if release_id not in self._releases_cache:
            self._releases_cache[release_id] = self.mb.get_release(release_id, includes=['release-groups'])
        return self._releases_cache[release_id]

    def _get_work(self, work_id: str) -> Work:
        """Get work, including recording relations, from cache or MusicBrainz"""
        if work_id not in self._works_cache:
            self._works_cache[work_id] = self._fetch_work(work_id)
        return self._works_cache[work_id]

    def _parse_date(self, date: Optional[str], source: str) -> Optional[DateWrapper]:
        if not date:
            return None
        try:
            return DateWrapper(iso_string=date)
        except ValueError:
            self._log.error('Could not parse date {0} for {1}', date, source)
            return None

    @staticmethod
    def _oldest(*dates: Optional[DateWrapper]) -> Optional[DateWrapper]:
        oldest = None
        for date in dates:
            if date is not None and (oldest is None or date < oldest):
                oldest = date
        return oldest

    def _recording_first_release_date(self, recording: Recording) -> Optional[DateWrapper]:
        """First release date of a recording, optionally only considering releases with given status"""
        release_types = self.config['release_types'].get()
        if not release_types:
            return self._parse_date(recording.get('first-release-date'), 'recording ' + str(recording.get('id')))

        oldest = None
        for release in recording.get('releases') or []:
            if release.get('status') in release_types:
                oldest = self._oldest(oldest, self._parse_date(release.get('date'),
                                                               'release ' + str(release.get('id'))))
        return oldest

    # Approaches

    def _release_date(self, item: Item) -> Optional[DateWrapper]:
        """Release approach: first release date of the release group of the item's album"""
        if not item.mb_albumid:
            self._log.warning('No mb_albumid for {0.artist} - {0.title}, cannot use release approach', item)
            return None
        release = self._get_release(item.mb_albumid)
        release_group = release.get('release-group') or {}
        return self._oldest(
            self._parse_date(release_group.get('first-release-date'), 'release group ' + str(release_group.get('id'))),
            None if release_group else self._parse_date(release.get('date'), 'release ' + item.mb_albumid))

    def _recording_date(self, recording_id: str) -> Optional[DateWrapper]:
        """Recording approach: first release date of the recording"""
        return self._recording_first_release_date(self._get_recording(recording_id))

    def _work_date(self, recording_id: str) -> Optional[DateWrapper]:
        """Work approach: oldest first release date of all recordings of the recording's work"""
        recording = self._get_recording(recording_id)
        oldest_date = self._recording_first_release_date(recording)

        work_id = self._get_work_id_from_recording(recording)
        if not work_id:  # Only look through this recording
            self._log.info('Recording {0} has no associated work, only using its own date', recording_id)
            return oldest_date

        work = self._get_work(work_id)
        recording_rels = mb_api.recording_relations(work)
        if not recording_rels:
            self._log.error(
                'Work {0} has no valid associated recordings! Please choose another recording or amend the data!',
                work_id)
            return oldest_date

        is_cover = self._is_cover(recording)
        artist_ids = self._get_artist_ids_from_recording(recording)

        for rel in recording_rels:
            rec = rel['recording']
            rec_id = rec.get('id')
            if not rec_id or rec_id == recording_id:
                continue
            attributes = rel.get('attributes') or []

            fetched: Optional[Recording] = None
            if is_cover:
                # If a cover, only keep covers by the same artist
                if 'cover' not in attributes:
                    continue
                fetched = self._get_recording(rec_id)
                if not self._contains_artist(fetched, artist_ids):
                    continue
            elif 'cover' in attributes or (attributes and self.config['filter_recordings']):
                # Remove covers and, if configured, recordings with attributes (e.g. live)
                continue

            if fetched is None:
                if 'first-release-date' in rec and not self.config['release_types'].get():
                    date = self._parse_date(rec.get('first-release-date'), 'recording ' + rec_id)
                    oldest_date = self._oldest(oldest_date, date)
                    continue
                fetched = self._get_recording(rec_id)
            oldest_date = self._oldest(oldest_date, self._recording_first_release_date(fetched))

        return oldest_date

    def _get_oldest_date(self, item: Item, approach: str) -> Optional[DateWrapper]:
        """Get oldest date for an item using given approach"""
        if approach == RELEASE:
            oldest_date = self._release_date(item)
        elif approach == RECORDING:
            oldest_date = self._recording_date(item.mb_trackid)
        else:
            oldest_date = self._work_date(item.mb_trackid)

        if self.config['use_file_date'] and item.year:
            oldest_date = self._oldest(oldest_date, DateWrapper(item.year, item.month or None, item.day or None))

        return oldest_date
