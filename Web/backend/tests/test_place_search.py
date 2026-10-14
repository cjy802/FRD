import unittest
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from main import app
from place_search import PlaceSearchError, search_places


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


class PlaceSearchTests(unittest.TestCase):
    def setUp(self):
        self.key_patch = patch('place_search._key', return_value='test-server-key')
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def test_poi_search_preserves_bd09_lat_lng_and_filters_invalid_candidates(self):
        with patch('place_search.requests.get', return_value=response({'status': 0, 'results': [
            {'name': '测试学校', 'address': '常州市测试路', 'uid': 'school', 'location': {'lat': 31.683938, 'lng': 119.96101}},
            {'name': '城市提示'}, {'name': '错误坐标', 'location': {'lat': 119.9, 'lng': 31.68}},
        ]})) as get:
            payload = search_places('测试学校', '常州市')
        self.assertEqual(len(payload['results']), 1)
        self.assertEqual(payload['results'][0]['location'], {'lat': 31.683938, 'lng': 119.96101})
        self.assertEqual(payload['results'][0]['coordinate_system'], 'bd09')
        self.assertNotIn('test-server-key', str(payload))
        self.assertTrue(get.call_args.args[0].endswith('place/v3/region'))
        self.assertEqual(get.call_args.kwargs['params']['region'], '常州市')

    def test_structured_address_uses_geocoding_and_reports_imprecise_result(self):
        with patch('place_search.requests.get', return_value=response({'status': 0,
            'result': {'location': {'lat': 31.68, 'lng': 119.96}, 'precise': 0, 'confidence': 50, 'level': '道路'},
            'poi_infos': [{'formatted_address': '常州市测试路10号'}],
        })) as get:
            item = search_places('常州市测试路10号')['results'][0]
        self.assertTrue(get.call_args.args[0].endswith('geocoding/v3/'))
        self.assertEqual(item['address'], '常州市测试路10号')
        self.assertEqual(item['precision']['precise'], 0)

    def test_empty_poi_search_falls_back_to_address_lookup(self):
        with patch('place_search.requests.get', side_effect=[response({'status': 0, 'results': []}),
            response({'status': 0, 'result': {'location': {'lat': 31.68, 'lng': 119.96}, 'precise': 1}})]) as get:
            self.assertEqual(len(search_places('测试园区')['results']), 1)
        self.assertEqual(get.call_count, 2)

    def test_disabled_service_is_503_without_exposing_upstream_message_or_key(self):
        with patch('place_search.requests.get', return_value=response({'status': 240, 'message': 'test-server-key'})) as get:
            result = TestClient(app).get('/api/places/search', params={'query': '测试学校'})
        self.assertEqual(result.status_code, 503)
        self.assertIn('240', result.json()['detail'])
        self.assertNotIn('test-server-key', result.text)
        self.assertEqual(get.call_count, 1, 'Permission errors must not trigger fallback API calls')

    def test_quota_and_timeout_have_distinct_errors(self):
        for failure, status in ((response({'status': 302}), 429), (requests.Timeout('secret request URL'), 504)):
            with self.subTest(status=status), patch('place_search.requests.get', **({'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure})):
                result = TestClient(app).get('/api/places/search', params={'query': '测试学校'})
                self.assertEqual(result.status_code, status)
                self.assertNotIn('secret request URL', result.text)

    def test_missing_key_does_not_make_request(self):
        self.key_patch.stop()
        with patch.dict('os.environ', {'BAIDU_WEB_SERVICE_AK': ''}), patch('place_search.dotenv_values', return_value={}), patch('place_search.requests.get') as get:
            result = TestClient(app).get('/api/places/search', params={'query': '测试学校'})
        self.assertEqual(result.status_code, 503)
        get.assert_not_called()

    def test_invalid_input_and_malformed_response(self):
        client = TestClient(app)
        for query in ('', '   ', 'a' * 129):
            with self.subTest(query=query), patch('place_search.requests.get') as get:
                self.assertEqual(client.get('/api/places/search', params={'query': query}).status_code, 422)
                get.assert_not_called()
        with patch('place_search.requests.get', return_value=response({'status': 0, 'results': 'invalid'})):
            self.assertEqual(client.get('/api/places/search', params={'query': '测试学校'}).status_code, 502)
        with self.assertRaises(PlaceSearchError):
            search_places('测试路' * 50)


if __name__ == '__main__':
    unittest.main()
