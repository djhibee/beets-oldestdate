"""
MusicBrainz JSON API fixtures (``/ws/2/...?fmt=json``) built around the "Thriller" recording
https://musicbrainz.org/recording/2eec3a3b-33af-4ea1-b169-7450b941732d

The recording was first released in 2005 (on a 2005 release group), while the "Thriller" work
was first released in 1982. Only the fields used by the plugin are kept; identifiers other than the
recording and artist ones are placeholders.
"""
import copy

MICHAEL_JACKSON_ID = 'f27ec8db-af05-4f36-916e-3d57f91ecf5e'
COVER_ARTIST_ID = 'cover-artist-id'

RECORDING_ID = '2eec3a3b-33af-4ea1-b169-7450b941732d'
ORIGINAL_RECORDING_ID = 'thriller-1982-recording-id'
LIVE_RECORDING_ID = 'thriller-live-recording-id'
COVER_RECORDING_ID = 'thriller-cover-recording-id'
RELEASE_ID = 'thriller-2005-release-id'
RELEASE_GROUP_ID = 'thriller-2005-release-group-id'
WORK_ID = 'thriller-work-id'


def _artist_credit(artist_id, name):
    return [{'name': name, 'joinphrase': '', 'artist': {'id': artist_id, 'name': name}}]


RECORDINGS = {
    RECORDING_ID: {
        'id': RECORDING_ID,
        'title': 'Thriller',
        'first-release-date': '2005',
        'artist-credit': _artist_credit(MICHAEL_JACKSON_ID, 'Michael Jackson'),
        'releases': [{'id': RELEASE_ID, 'title': 'Thriller', 'status': 'Official', 'date': '2005'}],
        'relations': [{'type': 'performance', 'target-type': 'work', 'attributes': [],
                       'work': {'id': WORK_ID, 'title': 'Thriller'}}],
    },
    ORIGINAL_RECORDING_ID: {
        'id': ORIGINAL_RECORDING_ID,
        'title': 'Thriller',
        'first-release-date': '1982-11-30',
        'artist-credit': _artist_credit(MICHAEL_JACKSON_ID, 'Michael Jackson'),
        'releases': [{'id': 'thriller-1982-release-id', 'status': 'Official', 'date': '1982-11-30'}],
        'relations': [{'type': 'performance', 'target-type': 'work', 'attributes': [],
                       'work': {'id': WORK_ID, 'title': 'Thriller'}}],
    },
    LIVE_RECORDING_ID: {
        'id': LIVE_RECORDING_ID,
        'title': 'Thriller (live)',
        'first-release-date': '1981',  # Fake older date, must be filtered out (live)
        'artist-credit': _artist_credit(MICHAEL_JACKSON_ID, 'Michael Jackson'),
        'releases': [{'id': 'live-release-id', 'status': 'Bootleg', 'date': '1981'}],
        'relations': [{'type': 'performance', 'target-type': 'work', 'attributes': ['live'],
                       'work': {'id': WORK_ID, 'title': 'Thriller'}}],
    },
    COVER_RECORDING_ID: {
        'id': COVER_RECORDING_ID,
        'title': 'Thriller',
        'first-release-date': '1980',  # Fake older date, must be filtered out (cover)
        'artist-credit': _artist_credit(COVER_ARTIST_ID, 'Cover Band'),
        'releases': [{'id': 'cover-release-id', 'status': 'Official', 'date': '1980'}],
        'relations': [{'type': 'performance', 'target-type': 'work', 'attributes': ['cover'],
                       'work': {'id': WORK_ID, 'title': 'Thriller'}}],
    },
}

RELEASES = {
    RELEASE_ID: {
        'id': RELEASE_ID,
        'title': 'Thriller',
        'status': 'Official',
        'date': '2005-10-17',
        'release-group': {'id': RELEASE_GROUP_ID, 'title': 'Thriller', 'primary-type': 'Album',
                          'secondary-types': ['Compilation'], 'first-release-date': '2005'},
    },
}


def _recording_rel(recording_id, attributes):
    recording = RECORDINGS[recording_id]
    return {'type': 'performance', 'target-type': 'recording', 'attributes': attributes,
            'recording': {'id': recording_id, 'title': recording['title'],
                          'first-release-date': recording['first-release-date']}}


WORKS = {
    WORK_ID: {
        'id': WORK_ID,
        'title': 'Thriller',
        'relations': [
            _recording_rel(RECORDING_ID, []),
            _recording_rel(ORIGINAL_RECORDING_ID, []),
            _recording_rel(LIVE_RECORDING_ID, ['live']),
            _recording_rel(COVER_RECORDING_ID, ['cover']),
        ],
    },
}


class FakeMusicBrainzClient:
    """Stand-in for mb_api.MusicBrainzClient serving the fixtures above"""

    def __init__(self):
        self.calls = []

    def _get(self, entities, entity, mbid, includes):
        self.calls.append((entity, mbid, tuple(includes)))
        from beetsplug.mb_api import MusicBrainzError
        if mbid not in entities:
            raise MusicBrainzError('HTTP 404 for {}/{}'.format(entity, mbid))
        return copy.deepcopy(entities[mbid])

    def get_recording(self, recording_id, includes=()):
        return self._get(RECORDINGS, 'recording', recording_id, includes)

    def get_release(self, release_id, includes=()):
        return self._get(RELEASES, 'release', release_id, includes)

    def get_work(self, work_id, includes=()):
        return self._get(WORKS, 'work', work_id, includes)
