import datetime
import os
import threading
import time
from typing import Optional, Any, List, Dict, Iterable, Tuple, Union
from urllib.parse import quote_plus

import mediafile
from beets import ui, config
from beets.autotag import hooks, TrackInfo
from beets.importer import Action, ImportTask, ImportSession
from beets.library import Item, Library
from beets.plugins import BeetsPlugin

from . import mb_api
from .date_wrapper import DateWrapper, parse_musicbrainz_date

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


class ScanTimedOut(Exception):
    """Raised when the scan of a track reaches max_scan_seconds.
    Carries the oldest date found so far, so that it can still be used."""

    def __init__(self, phase: str, partial_date: Optional[DateWrapper] = None) -> None:
        super().__init__('scan time limit reached while ' + phase)
        self.phase = phase
        self.partial_date = partial_date


SKIPPED_LOG_NAME = 'oldestdate-skipped.txt'


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
            'prompt_for': [],  # Approaches for which to confirm, change approach or skip an item/album, e.g. [work]
            'release_types': None,  # Filter by release status, e.g. ['Official']
            'use_file_date': False,  # Also use file's embedded date when looking for oldest date
            'max_network_retries': 3,  # Maximum amount of times a given network call will be retried
            'show_progress': False,  # Print per-track progress while fetching works, recordings and releases
            'progress_every': 1,  # When showing progress, print work scan status every N related recordings
            'max_scan_seconds': 120,  # Time budget per track, then keep oldest date found so far. 0: no limit
            'max_related_recordings': 200,  # Work approach: scan at most N related recordings. 0: no limit
            'minimum_file_year': 1000,  # Embedded years below this (0, blank, absurd) are treated as unknown
        })

        self._recordings_cache: Dict[str, Recording] = dict()
        self._releases_cache: Dict[str, Release] = dict()
        self._works_cache: Dict[str, Work] = dict()
        self._deadline: Optional[float] = None
        self._scanning = False
        self._skip_log_lock = threading.Lock()

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
                    task.choice_flag = Action.SKIP
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
            work_id = work_rel['work'].get('id')
            if isinstance(work_id, str) and work_id:
                return work_id
        return None

    def _contains_artist(self, recording: Recording, artist_ids: List[str]) -> bool:
        """Returns whether this recording contains at least one of the specified artists"""
        return any(artist_id in artist_ids for artist_id in mb_api.artist_ids(recording))

    def _get_artist_ids_from_recording(self, recording: Recording) -> List[str]:
        """Extract artist ids from a recording"""
        return mb_api.artist_ids(recording)

    def _is_cover(self, recording: Recording) -> bool:
        """Returns whether given fetched recording is a cover of a work"""
        return any('cover' in mb_api.relation_attributes(rel) for rel in mb_api.work_relations(recording))

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
        If the approach is listed in prompt_for, first ask to confirm, change approach or skip."""
        by_approach: Dict[str, List[Item]] = {}
        for item in items:
            if self._should_process(item):
                by_approach.setdefault(forced_approach or self._get_approach(item), []).append(item)

        prompt_for = self._prompt_for()
        for approach, approach_items in by_approach.items():
            if approach in prompt_for:
                selected_approach = self._select_approach(approach_items, approach)
                if selected_approach is None:
                    self._log.info('Skipping {0} item(s) as requested ({1} approach)', len(approach_items), approach)
                    continue
                approach = selected_approach
            for item in approach_items:
                result = self._find_date(item, approach)
                if result is not None:
                    self._apply_date(item, result[0], approach, *result[1:])

    def _process_file(self, item: Item, forced_approach: Optional[str] = None) -> None:
        self._process_items([item], forced_approach)

    def _prompt_for(self) -> List[str]:
        value = self.config['prompt_for'].get() or []
        if isinstance(value, str):
            value = [value]
        return [normalize_approach(approach) for approach in value]

    def _select_approach(self, items: List[Item], approach: str) -> Optional[str]:
        """Confirm or change the approach for an album or singleton, or return None to skip"""
        if config['import']['quiet'].get(bool):
            return approach
        first = items[0]
        if first.album_id is not None:
            description = 'album {} - {} ({} track(s))'.format(first.albumartist or first.artist, first.album,
                                                               len(items))
        else:
            description = 'track {} - {}'.format(first.artist, first.title)
        print('oldestdate: process {} using the {} approach?'.format(description, approach))
        alternatives = [(RELEASE, 'Release'), (RECORDING, 'reCording'), (WORK, 'Work')]
        options = ['Yes'] + [label for name, label in alternatives if name != approach] + ['Skip']
        sel = ui.input_options(options, default='y')
        if sel == 'y':
            return approach
        return {'r': RELEASE, 'c': RECORDING, 'w': WORK}.get(sel)

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

    def _status(self, message: str) -> None:
        """Print progress if show_progress is enabled, otherwise only log it at debug level"""
        if self.config['show_progress'].get(bool):
            ui.print_('oldestdate: ' + message)
        else:
            self._log.debug(message)

    def _check_deadline(self, phase: str) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise ScanTimedOut(phase)

    def _find_date(self, item: Item, approach: str) -> Optional[Tuple[DateWrapper, float, bool]]:
        """Find oldest date of an item using given approach.
        Returns the date, elapsed seconds and whether the scan timed out, or None if skipped."""
        label = '{} - {}'.format(item.artist, item.title)
        started = time.monotonic()
        max_seconds = float(self.config['max_scan_seconds'].get() or 0)
        self._deadline = started + max_seconds if max_seconds > 0 else None
        self._scanning = True
        self._status('Starting: {} ({} approach)'.format(label, approach))

        timed_out = False
        try:
            oldest_date = self._get_oldest_date(item, approach)
        except ScanTimedOut as e:
            elapsed = time.monotonic() - started
            if e.partial_date is None:
                self._skip_item(item, 'scan timed out after {:.1f}s while {} before a usable date was found ({} '
                                      'approach)'.format(elapsed, e.phase, approach))
                return None
            self._status('Time limit reached for {} after {:.1f}s while {}; using oldest date found so far: {}'.format(
                label, elapsed, e.phase, format_date(e.partial_date)))
            oldest_date, timed_out = e.partial_date, True
        except mb_api.MusicBrainzError as e:
            self._skip_item(item, 'MusicBrainz request failed: {}'.format(e))
            return None
        except Exception as e:  # Malformed data or unexpected error must not break the whole import
            self._log.debug('Error while processing {0}', label, exc_info=True)
            self._skip_item(item, '{}: {}'.format(type(e).__name__, e))
            return None
        finally:
            self._deadline = None
            self._scanning = False
            self._recordings_cache.clear()

        if not oldest_date:
            self._skip_item(item, 'no usable date found ({} approach)'.format(approach))
            return None

        return oldest_date, time.monotonic() - started, timed_out

    # Skipped tracks log

    def _skip_log_path(self) -> Optional[str]:
        """Path of the skipped-track log: <beets directory>/oldestdate-skipped.txt"""
        directory = config['directory'].get()
        if not directory:
            return None
        return os.path.join(os.path.abspath(os.path.expanduser(os.fsdecode(directory))), SKIPPED_LOG_NAME)

    def _skip_item(self, item: Item, reason: str) -> None:
        """Report a track that could not be processed, and append it to the skipped-track log"""
        self._log.error('Skipping {0.artist} - {0.title}: {1}', item, reason)
        log_path = self._skip_log_path()
        if not log_path:
            self._log.warning('No beets directory configured, skipped-track log not written')
            return
        line = '{} | {} - {} | {} | {}\n'.format(
            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), item.artist, item.title,
            os.fsdecode(item.path) if item.path else '', reason)
        try:
            with self._skip_log_lock:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, 'a', encoding='utf-8') as handle:
                    handle.write(line)
        except OSError as e:
            self._log.error('Could not append to skipped-track log {0}: {1}', log_path, e)

    def _overwrite_date(self, approach: str) -> bool:
        """Whether date fields must be overwritten for given approach"""
        value = self.config['overwrite_date'].get()
        if isinstance(value, bool) or value is None:
            return bool(value)
        if isinstance(value, str):
            value = [value]
        return approach in [normalize_approach(a) for a in value]

    def _apply_date(self, item: Item, oldest_date: DateWrapper, approach: str,
                    elapsed: float = 0.0, timed_out: bool = False) -> None:
        if oldest_date.y is not None:
            item['recording_year'] = oldest_date.y
        if oldest_date.m is not None:
            item['recording_month'] = oldest_date.m
        if oldest_date.d is not None:
            item['recording_day'] = oldest_date.d

        summary = '{} approach; elapsed: {:.1f}s; timed out: {}'.format(approach, elapsed, 'yes' if timed_out else 'no')

        # Write over the date tag if configured as YYYYMMDD
        if self._overwrite_date(approach):
            self._log.warning('Overwriting date field for: {0.artist} - {0.title} from {0.year}-{0.month}-{0.day} '
                              'to {1} [{2}]', item, format_date(oldest_date), summary)
            item.year = str(oldest_date.y).zfill(4)
            item.month = "" if (oldest_date.m is None or not self.config['overwrite_month']) \
                else str(oldest_date.m).zfill(2)
            item.day = "" if (oldest_date.d is None or not self.config['overwrite_day']) \
                else str(oldest_date.d).zfill(2)
        else:
            ui.print_('oldestdate: Oldest date for: {} - {} is {} [{}]'.format(
                item.artist, item.title, format_date(oldest_date), summary))

        self._log.debug('Applying changes to {0.artist} - {0.title}', item)
        item.store()
        # Prevent changing file on disk before it reaches final destination
        if not self._importing:
            item.write()

    # MusicBrainz data

    def _before_fetch(self, label: str) -> None:
        """Check the scan time budget and report progress before a MusicBrainz call"""
        self._check_deadline(label)
        if self._scanning:
            self._status(label)

    def _fetch_recording(self, recording_id: str, label: str = 'recording') -> Recording:
        """Fetch and cache recording from MusicBrainz, including artists and work relations"""
        includes = ['artists', 'work-rels']
        if self.config['release_types'].get():
            includes.append('releases')  # Needed to filter dates by release status
        self._before_fetch('Fetching {} {}'.format(label, recording_id))
        recording: Recording = mb_api.mapping(self.mb.get_recording(recording_id, includes=includes))

        self._recordings_cache[recording_id] = recording
        return recording

    def _get_recording(self, recording_id: str, label: str = 'recording') -> Recording:
        """Get recording from cache or MusicBrainz"""
        return self._recordings_cache[
            recording_id] if recording_id in self._recordings_cache else self._fetch_recording(recording_id, label)

    def _get_release(self, release_id: str) -> Release:
        """Get release, including its release group, from cache or MusicBrainz"""
        if release_id not in self._releases_cache:
            self._before_fetch('Fetching release {}'.format(release_id))
            self._releases_cache[release_id] = mb_api.mapping(
                self.mb.get_release(release_id, includes=['release-groups']))
        return self._releases_cache[release_id]

    def _get_work(self, work_id: str) -> Work:
        """Get work, including recording relations, from cache or MusicBrainz"""
        if work_id not in self._works_cache:
            self._before_fetch('Fetching work {}'.format(work_id))
            self._works_cache[work_id] = mb_api.mapping(self._fetch_work(work_id))
        return self._works_cache[work_id]

    def _parse_date(self, date: Any, source: str) -> Optional[DateWrapper]:
        if not date:
            return None
        parsed = parse_musicbrainz_date(date)
        if parsed is None:
            self._log.debug('Ignoring unusable date {0!r} for {1}', date, source)
        return parsed

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
        for release in map(mb_api.mapping, recording.get('releases') or []):
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
        release_group = mb_api.mapping(release.get('release-group'))
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
        try:
            return self._scan_work(recording_id, recording, oldest_date)
        except ScanTimedOut as e:
            # Keep the oldest valid date found before the time budget was reached
            raise ScanTimedOut(e.phase, self._oldest(e.partial_date, oldest_date))

    def _scan_work(self, recording_id: str, recording: Recording,
                   oldest_date: Optional[DateWrapper]) -> Optional[DateWrapper]:
        work_id = self._get_work_id_from_recording(recording)
        if not work_id:  # Only look through this recording
            self._status('Recording {} has no associated work, only using its own date'.format(recording_id))
            return oldest_date

        work = self._get_work(work_id)
        recording_rels = [rel for rel in mb_api.recording_relations(work)
                          if rel['recording']['id'] != recording_id]
        if not recording_rels:
            self._log.info('Work {0} has no other associated recordings, only using recording date', work_id)
            return oldest_date

        total = len(recording_rels)
        max_related = int(self.config['max_related_recordings'].get() or 0)
        if 0 < max_related < total:
            self._status('Work {} has {} related recordings; scanning the first {} (max_related_recordings)'.format(
                work_id, total, max_related))
            recording_rels = recording_rels[:max_related]
            total = max_related
        else:
            self._status('Work {} has {} related recordings'.format(work_id, total))

        is_cover = self._is_cover(recording)
        artist_ids = self._get_artist_ids_from_recording(recording)
        every = max(1, int(self.config['progress_every'].get() or 1))

        for index, rel in enumerate(recording_rels, 1):
            try:
                self._check_deadline('scanning related recordings ({}/{})'.format(index, total))
                if index == 1 or index == total or index % every == 0:
                    self._status('Work scan {}/{}; current oldest: {}'.format(
                        index, total, format_date(oldest_date) if oldest_date else 'none'))
                oldest_date = self._oldest(oldest_date, self._related_recording_date(
                    rel, is_cover, artist_ids, 'related recording {}/{}'.format(index, total)))
            except ScanTimedOut as e:
                raise ScanTimedOut(e.phase, self._oldest(e.partial_date, oldest_date))

        return oldest_date

    def _related_recording_date(self, rel: Dict[str, Any], is_cover: bool, artist_ids: List[str],
                                label: str) -> Optional[DateWrapper]:
        """Date of a recording related to the work, or None if it must be filtered out"""
        rec = rel['recording']
        rec_id = rec['id']
        attributes = mb_api.relation_attributes(rel)

        fetched: Optional[Recording] = None
        if is_cover:
            # If a cover, only keep covers by the same artist
            if 'cover' not in attributes:
                return None
            fetched = self._get_recording(rec_id, label)
            if not self._contains_artist(fetched, artist_ids):
                return None
        elif 'cover' in attributes or (attributes and self.config['filter_recordings']):
            # Remove covers and, if configured, recordings with attributes (e.g. live)
            return None

        if fetched is None:
            if 'first-release-date' in rec and not self.config['release_types'].get():
                return self._parse_date(rec.get('first-release-date'), 'recording ' + rec_id)
            fetched = self._get_recording(rec_id, label)
        return self._recording_first_release_date(fetched)

    def _get_oldest_date(self, item: Item, approach: str) -> Optional[DateWrapper]:
        """Get oldest date for an item using given approach"""
        file_date = self._item_date_or_none(item) if self.config['use_file_date'] else None
        try:
            if approach == RELEASE:
                oldest_date = self._release_date(item)
            elif approach == RECORDING:
                oldest_date = self._recording_date(item.mb_trackid)
            else:
                oldest_date = self._work_date(item.mb_trackid)
        except ScanTimedOut as e:
            raise ScanTimedOut(e.phase, self._oldest(e.partial_date, file_date))

        return self._oldest(oldest_date, file_date)

    def _item_date_or_none(self, item: Item) -> Optional[DateWrapper]:
        """Embedded date of the item, or None if missing, zero or implausibly old (below minimum_file_year)"""
        try:
            year = int(item.year or 0)
        except (TypeError, ValueError):
            return None
        if year < max(1, int(self.config['minimum_file_year'].get() or 1)) or year > datetime.MAXYEAR:
            return None

        def component(value: Any, upper: int) -> Optional[int]:
            try:
                number = int(value or 0)
            except (TypeError, ValueError):
                return None
            return number if 0 < number <= upper else None

        month = component(item.month, 12)
        day = component(item.day, 31) if month else None
        return DateWrapper(year, month, day)
