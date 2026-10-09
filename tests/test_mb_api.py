import unittest
from unittest import mock

import requests

from beetsplug import mb_api


def _response(status=200, payload=None):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = payload if payload is not None else {}
    return response


class MusicBrainzClientTest(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.session.headers = {}
        self.sleep = mock.Mock()
        self.client = mb_api.MusicBrainzClient(host='musicbrainz.org', https=True, ratelimit_interval=0,
                                               max_retries=3, session=self.session, sleep=self.sleep)

    def test_user_agent_and_json_headers(self):
        self.assertIn('beets-oldestdate', self.session.headers['User-Agent'])
        self.assertEqual('application/json', self.session.headers['Accept'])

    def test_get_recording_builds_url(self):
        self.session.get.return_value = _response(payload={'id': 'abc'})
        result = self.client.get_recording('abc', includes=['artists', 'work-rels'])
        self.assertEqual({'id': 'abc'}, result)
        self.session.get.assert_called_once_with(
            'https://musicbrainz.org/ws/2/recording/abc',
            params={'inc': 'artists+work-rels', 'fmt': 'json'}, timeout=30.0)

    def test_http_host(self):
        client = mb_api.MusicBrainzClient(host='localhost:5000', https=False, session=self.session)
        self.assertEqual('http://localhost:5000/ws/2', client.base_url)

    def test_retry_on_unavailable(self):
        self.session.get.side_effect = [_response(503), _response(payload={'id': 'w'})]
        self.assertEqual({'id': 'w'}, self.client.get_work('w'))
        self.assertEqual(2, self.session.get.call_count)

    def test_retry_on_connection_error_then_give_up(self):
        self.session.get.side_effect = requests.ConnectionError('boom')
        with self.assertRaises(mb_api.NetworkError):
            self.client.get_release('r')
        self.assertEqual(3, self.session.get.call_count)

    def test_not_found_is_not_retried(self):
        self.session.get.return_value = _response(404)
        with self.assertRaises(mb_api.MusicBrainzError):
            self.client.get_release('r')
        self.assertEqual(1, self.session.get.call_count)

    def test_helpers(self):
        recording = {
            'artist-credit': [{'name': 'A', 'artist': {'id': 'a1'}}, {'name': 'B', 'artist': {'id': 'a2'}}],
            'relations': [{'target-type': 'url', 'url': {}},
                          {'target-type': 'work', 'work': {'id': 'w1'}, 'attributes': ['cover']}],
        }
        self.assertEqual(['a1', 'a2'], mb_api.artist_ids(recording))
        self.assertEqual('w1', mb_api.work_relations(recording)[0]['work']['id'])
        work = {'relations': [{'target-type': 'recording', 'recording': {'id': 'r1'}},
                              {'target-type': 'artist', 'artist': {'id': 'x'}}]}
        self.assertEqual(1, len(mb_api.recording_relations(work)))


if __name__ == '__main__':
    unittest.main()
