from typing import Optional, Any, List, Dict, Iterable, Tuple, Union
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
            'overwrite_date': False,  # Overwrite date field in tags
            'overwrite_month': True,  # If overwriting date, also overwrite month field
            'overwrite_day': True,  # If overwriting date and month, also overwrite day
            'filter_recordings': True,  # Work approach: skip recordings with attributes (e.g. live)
            'singleton': RECORDING,  # Approach for singletons
            'album': RELEASE,  # Approach for standard albums
            'compilation': RECORDING,  # Approach for compilations
            'album_type': {},  # Approach by album type, e.g. {soundtrack: release}. Takes priority
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

    def _import_task_choice(self, task: ImportTask, session: ImportSession) -> None:
        match = task.match
        if not match:
            return
        match = match.info

        recording_id = match.track_id
        search_link = "https://musicbrainz.org/search?query=" + match.title.replace(' ', '+') \
                      + "+artist%3A%22" + match.artist.replace(' ', '+') \
                      + "%22&type=recording&limit=100&method=advanced"

        while not self._has_work_id(recording_id):
            recording_date = self._recording_date(recording_id)
            recording_year_string = None if recording_date is None else recording_date.strftime('%Y-%m-%d')

            self._log.error("{0.artist} - {0.title} ({1}) has no associated work! Please fix "
                            "and try again!", match,
                            recording_year_string)
            print("Search link: " + search_link)
            sel = ui.input_options(('Use this recording', 'Try again', 'Skip track'))

            if sel == "t":  # Fetch data again
                self._fetch_recording(recording_id)
            elif sel == "u":
                return
            else:
                task.choice_flag = action.SKIP
                return

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
    def _item_album_types(item: Item) -> List[str]:
        types: List[str] = []
        album_types: Union[None, str, List[str]] = item.get('albumtypes')
        if isinstance(album_types, str):
            album_types = album_types.split(';')
        for album_type in list(album_types or []) + [item.get('albumtype') or '']:
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
        return self._approach_for(item.album_id is None, bool(item.get('comp')), self._item_album_types(item))

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

    def _command_func(self, lib: Library, _: ImportSession, args: List[str]) -> None:
        """This queries the local database, not the files."""
        self._importing = False
        for item in lib.items(args):
            self._process_file(item)

    def _on_import(self, _: ImportSession, task: ImportTask) -> None:
        if self.config['auto']:
            self._importing = True
            for item in task.imported_items():
                self._process_file(item)

    def _process_file(self, item: Item) -> None:
        if not item.mb_trackid or item.data_source != 'MusicBrainz':
            self._log.info('Skipping track with no mb_trackid: {0.artist} - {0.title}', item)
            return

        # Check for the recording_year and if it exists and not empty skips the track (if force is not True)
        if 'recording_year' in item and item.recording_year and not self.config['force']:
            self._log.info('Skipping already processed track: {0.artist} - {0.title}', item)
            return

        approach = self._get_approach(item)

        # Get oldest date from MusicBrainz
        try:
            oldest_date = self._get_oldest_date(item, approach)
        except mb_api.MusicBrainzError as e:
            self._log.error('Could not fetch data from MusicBrainz for {0.artist} - {0.title}: {1}', item, e)
            return
        finally:
            self._recordings_cache.clear()

        if not oldest_date:
            self._log.error('No date found for {0.artist} - {0.title} ({1} approach)', item, approach)
            return

        self._log.info('Oldest date for {0.artist} - {0.title} ({1} approach): {2}', item, approach,
                       oldest_date.strftime('%Y-%m-%d'))
        self._apply_date(item, oldest_date)

    def _apply_date(self, item: Item, oldest_date: DateWrapper) -> None:
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

        if self.config['overwrite_date']:
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
