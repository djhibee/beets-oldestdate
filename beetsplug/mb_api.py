"""Minimal client for the MusicBrainz JSON web service (https://musicbrainz.org/doc/MusicBrainz_API)."""
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests

JSON = Dict[str, Any]

VERSION = '2.0.0'  # Also change in pyproject.toml
USER_AGENT = 'beets-oldestdate/{} ( https://github.com/kernitus/beets-oldestdate )'.format(VERSION)

# HTTP status codes that are worth retrying (rate limiting / temporary server issues)
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


class MusicBrainzError(Exception):
    """Generic error while querying MusicBrainz (e.g. entity not found)."""


class NetworkError(MusicBrainzError):
    """Transient error (connection problem, rate limiting, server unavailable)."""


class MusicBrainzClient:
    def __init__(self, host: str = 'musicbrainz.org', https: bool = True, ratelimit: int = 1,
                 ratelimit_interval: float = 1.0, max_retries: int = 3, timeout: float = 30.0,
                 log: Optional[logging.Logger] = None,
                 session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        scheme = 'https' if https else 'http'
        self.base_url = '{}://{}/ws/2'.format(scheme, host.rstrip('/'))
        self.min_interval = ratelimit_interval / ratelimit if ratelimit > 0 else 0.0
        self.max_retries = max(1, max_retries)
        self.timeout = timeout
        self._log = log or logging.getLogger(__name__)
        self._session = session or requests.Session()
        self._session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})
        self._sleep = sleep
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _wait_rate_limit(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()

    def _request(self, path: str, params: Dict[str, str]) -> JSON:
        url = '{}/{}'.format(self.base_url, path)
        params = dict(params, fmt='json')
        for attempt in range(self.max_retries):
            self._wait_rate_limit()
            try:
                response = self._session.get(url, params=params, timeout=self.timeout)
                if response.status_code in RETRY_STATUS_CODES:
                    raise NetworkError('HTTP {} for {}'.format(response.status_code, url))
                if response.status_code != 200:
                    raise MusicBrainzError('HTTP {} for {}'.format(response.status_code, url))
                result: JSON = response.json()
                return result
            except (requests.ConnectionError, requests.Timeout, NetworkError) as e:
                if attempt < self.max_retries - 1:  # No need to wait after the last attempt
                    delay = 2 ** attempt
                    self._log.info('Network call failed ({0}), attempt {1}/{2}. Trying again in {3}s',
                                   e, attempt + 1, self.max_retries, delay)
                    self._sleep(delay)  # Exponential backoff each attempt
                elif isinstance(e, NetworkError):
                    raise
                else:
                    raise NetworkError(str(e)) from e
            except ValueError as e:  # Invalid JSON
                raise MusicBrainzError('Invalid response from {}: {}'.format(url, e)) from e
        raise AssertionError('Unreachable code')

    def get_entity(self, entity: str, mbid: str, includes: Sequence[str] = ()) -> JSON:
        params = {'inc': '+'.join(includes)} if includes else {}
        return self._request('{}/{}'.format(entity, mbid), params)

    def get_recording(self, recording_id: str, includes: Sequence[str] = ('artists', 'work-rels')) -> JSON:
        return self.get_entity('recording', recording_id, includes)

    def get_release(self, release_id: str, includes: Sequence[str] = ('release-groups',)) -> JSON:
        return self.get_entity('release', release_id, includes)

    def get_work(self, work_id: str, includes: Sequence[str] = ('recording-rels',)) -> JSON:
        return self.get_entity('work', work_id, includes)


# Helpers to extract data from MusicBrainz JSON entities.
# Never assume every entry is a mapping: e.g. artist-credit lists may contain plain join phrases (" feat. ").

def mapping(value: Any) -> JSON:
    """Return value if it is a mapping, an empty mapping otherwise"""
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def work_relations(recording: Any) -> List[JSON]:
    """Return the work relations of a recording"""
    return [rel for rel in map(mapping, _list(mapping(recording).get('relations')))
            if rel.get('target-type') == 'work' and mapping(rel.get('work'))]


def recording_relations(work: Any) -> List[JSON]:
    """Return the recording relations of a work"""
    return [rel for rel in map(mapping, _list(mapping(work).get('relations')))
            if rel.get('target-type') == 'recording' and mapping(rel.get('recording')).get('id')]


def relation_attributes(relation: Any) -> List[str]:
    """Return the attributes of a relation (e.g. cover, live)"""
    return [attribute for attribute in _list(mapping(relation).get('attributes')) if isinstance(attribute, str)]


def artist_ids(entity: Any) -> List[str]:
    """Return the artist ids from the artist credit of an entity"""
    ids = []
    for credit in _list(mapping(entity).get('artist-credit')):
        artist_id = mapping(mapping(credit).get('artist')).get('id')
        if isinstance(artist_id, str) and artist_id:
            ids.append(artist_id)
    return ids
