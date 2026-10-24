import unittest
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from main import app
from place_search import PlaceSearchError, reverse_geocode, search_nearby_places, search_places
from auth_test_support import login_admin


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
            client = TestClient(app)
            login_admin(client)
            result = client.get('/api/places/search', params={'query': '测试学校'})
        self.assertEqual(result.status_code, 503)
        self.assertIn('240', result.json()['detail'])
        self.assertNotIn('test-server-key', result.text)
        self.assertEqual(get.call_count, 1, 'Permission errors must not trigger fallback API calls')

    def test_quota_and_timeout_have_distinct_errors(self):
        for failure, status in ((response({'status': 302}), 429), (requests.Timeout('secret request URL'), 504)):
            with self.subTest(status=status), patch('place_search.requests.get', **({'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure})):
                client = TestClient(app)
                login_admin(client)
                result = client.get('/api/places/search', params={'query': '测试学校'})
                self.assertEqual(result.status_code, status)
                self.assertNotIn('secret request URL', result.text)

    def test_missing_key_does_not_make_request(self):
        self.key_patch.stop()
        with patch.dict('os.environ', {'BAIDU_WEB_SERVICE_AK': ''}), patch('place_search.dotenv_values', return_value={}), patch('place_search.requests.get') as get:
            client = TestClient(app)
            login_admin(client)
            result = client.get('/api/places/search', params={'query': '测试学校'})
        self.assertEqual(result.status_code, 503)
        get.assert_not_called()

    def test_invalid_input_and_malformed_response(self):
        client = TestClient(app)
        login_admin(client)
        for query in ('', '   ', 'a' * 129):
            with self.subTest(query=query), patch('place_search.requests.get') as get:
                self.assertEqual(client.get('/api/places/search', params={'query': query}).status_code, 422)
                get.assert_not_called()
        with patch('place_search.requests.get', return_value=response({'status': 0, 'results': 'invalid'})):
            self.assertEqual(client.get('/api/places/search', params={'query': '测试学校'}).status_code, 502)
        with self.assertRaises(PlaceSearchError):
            search_places('测试路' * 50)

    def test_nearby_search_uses_wgs84_and_preserves_bd09_results(self):
        with patch('place_search.requests.get', return_value=response({'status': 0, 'results': [
            {'name': '测试咖啡馆', 'address': '测试路1号', 'uid': 'coffee',
             'location': {'lat': 31.68, 'lng': 119.96}, 'detail_info': {'distance': 325}},
            {'name': '无效地点', 'location': {'lat': 999, 'lng': 119.96}},
        ]})) as get:
            results = search_nearby_places(' 咖啡馆 ', 31.6792, 119.9515)['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['location'], {'lat': 31.68, 'lng': 119.96})
        self.assertEqual(results[0]['coordinate_system'], 'bd09')
        self.assertEqual(results[0]['distance_m'], 325)
        self.assertEqual(results[0]['address'], '测试路1号')
        self.assertNotIn('test-server-key', str(results))
        self.assertTrue(get.call_args.args[0].endswith('place/v3/around'))
        params = get.call_args.kwargs['params']
        self.assertEqual(params['query'], '咖啡馆')
        self.assertEqual(params['location'], '31.6792,119.9515')
        self.assertEqual(params['coord_type'], 1)
        self.assertEqual(params['radius'], 1000)
        self.assertEqual(params['radius_limit'], 'true')
        self.assertEqual(params['scope'], 2)
        self.assertEqual(params['page_size'], 10)

    def test_reverse_geocode_prefers_poi_address_and_falls_back_to_street_address(self):
        with patch('place_search.requests.get', side_effect=[
            response({'status': 0, 'result': {'formatted_address_poi': '江苏省常州市测试咖啡馆',
                                              'formatted_address': '江苏省常州市测试路'}}),
            response({'status': 0, 'result': {'formatted_address': '江苏省常州市测试路'}}),
        ]) as get:
            self.assertEqual(reverse_geocode(31.6792, 119.9515), {
                'address': '江苏省常州市测试咖啡馆', 'place_name': '', 'nearby_results': [],
            })
            self.assertEqual(reverse_geocode(31.6792, 119.9515), {
                'address': '江苏省常州市测试路', 'place_name': '', 'nearby_results': [],
            })
        self.assertTrue(get.call_args.args[0].endswith('reverse_geocoding/v3/'))
        params = get.call_args.kwargs['params']
        self.assertEqual(params['location'], '31.6792,119.9515')
        self.assertEqual(params['coordtype'], 'wgs84ll')
        self.assertEqual(params['extensions_poi'], 1)
        self.assertEqual(params['radius'], 1000)

    def test_reverse_geocode_uses_containing_region_and_normalizes_nearby_pois(self):
        payload = {'status': 0, 'result': {
            'formatted_address_poi': '江苏省常州市测试园区',
            'poiRegions': [
                {'name': '附近但不在其中', 'direction_desc': '东北100米', 'distance': '100'},
                {'name': '测试园区', 'direction_desc': '内', 'distance': '0'},
            ],
            'pois': [
                {'name': '测试咖啡馆', 'addr': '测试路1号', 'uid': 'coffee', 'distance': '25',
                 'point': {'x': 119.96, 'y': 31.68}},
                {'name': '超范围地点', 'distance': '1100', 'point': {'x': 119.97, 'y': 31.69}},
                {'name': '错误坐标', 'distance': '10', 'point': {'x': '119.96', 'y': 31.68}},
                {'name': 42, 'distance': '10', 'point': {'x': 119.96, 'y': 31.68}},
            ],
        }}
        with patch('place_search.requests.get', return_value=response(payload)):
            result = reverse_geocode(31.6792, 119.9515)
        self.assertEqual(result['address'], '江苏省常州市测试园区')
        self.assertEqual(result['place_name'], '测试园区')
        self.assertEqual(result['nearby_results'], [{
            'name': '测试咖啡馆', 'address': '测试路1号', 'uid': 'coffee',
            'location': {'lat': 31.68, 'lng': 119.96}, 'coordinate_system': 'bd09',
            'distance_m': 25,
        }])
        self.assertNotIn('test-server-key', str(result))

    def test_reverse_geocode_selects_nearest_poi_and_safe_fallback_names(self):
        cases = [
            ({'poiRegions': [{'name': '不在其中', 'direction_desc': '东北100米'}],
              'pois': [{'name': '较远', 'distance': '150'},
                       {'name': '最近', 'distance': '20'},
                       {'name': '距离无效', 'distance': 'nan'}]}, '最近'),
            ({'business_info': [{'name': '测试商圈'}]}, '测试商圈'),
            ({'addressComponent': {'street': '测试路', 'town': '测试镇', 'district': '新北区'}}, '测试路'),
            ({'addressComponent': {'street': None, 'town': '测试镇', 'district': '新北区'}}, '测试镇'),
            ({'addressComponent': {'district': '新北区'}}, '新北区'),
            ({'poiRegions': [{'name': '园' * 50, 'direction_desc': '内'}],
              'business_info': [{'name': '短商圈'}]}, '短商圈'),
        ]
        for fields, expected in cases:
            with self.subTest(expected=expected), patch('place_search.requests.get', return_value=response({
                'status': 0, 'result': {'formatted_address': '江苏省常州市测试路', **fields},
            })):
                result = reverse_geocode(31.6792, 119.9515)
            self.assertEqual(result['place_name'], expected)
            self.assertLessEqual(len(result['place_name'].encode('utf-8')), 128)

    def test_reverse_nearby_results_are_capped_at_ten_valid_points(self):
        pois = [{'name': f'地点{index}', 'distance': str(index), 'point': {'x': 119.96, 'y': 31.68}}
                for index in range(12)]
        with patch('place_search.requests.get', return_value=response({
            'status': 0, 'result': {'formatted_address': '江苏省常州市测试路', 'pois': pois},
        })):
            result = reverse_geocode(31.6792, 119.9515)
        self.assertEqual(len(result['nearby_results']), 10)
        self.assertEqual(result['nearby_results'][0]['name'], '地点0')
        self.assertEqual(result['nearby_results'][9]['name'], '地点9')
        self.assertEqual(result['place_name'], '地点0')

    def test_nearby_and_reverse_reject_invalid_inputs_without_upstream_requests(self):
        with patch('place_search.requests.get') as get:
            for query, latitude, longitude in (
                (' ', 31.68, 119.96), ('a' * 129, 31.68, 119.96),
                ('咖啡馆', float('nan'), 119.96), ('咖啡馆', 31.68, float('inf')),
                ('咖啡馆', True, 119.96), ('咖啡馆', 91, 119.96),
                ('咖啡馆', 31.68, 181),
            ):
                with self.subTest(query=query, latitude=latitude, longitude=longitude):
                    with self.assertRaises(PlaceSearchError) as error:
                        search_nearby_places(query, latitude, longitude)
                    self.assertEqual(error.exception.status_code, 422)
            with self.assertRaises(PlaceSearchError) as error:
                reverse_geocode(-91, 119.96)
            self.assertEqual(error.exception.status_code, 422)
            get.assert_not_called()

    def test_nearby_and_reverse_reject_malformed_upstream_results(self):
        with patch('place_search.requests.get', return_value=response({'status': 0, 'results': 'invalid'})):
            with self.assertRaises(PlaceSearchError) as error:
                search_nearby_places('咖啡馆', 31.68, 119.96)
            self.assertEqual(error.exception.status_code, 502)
        with patch('place_search.requests.get', return_value=response({'status': 0, 'result': {}})):
            with self.assertRaises(PlaceSearchError) as error:
                reverse_geocode(31.68, 119.96)
            self.assertEqual(error.exception.status_code, 502)

    def test_nearby_and_reverse_http_routes(self):
        client = TestClient(app)
        login_admin(client)
        with patch('place_search.requests.get', side_effect=[
            response({'status': 0, 'results': [{'name': '测试公园', 'location': {'lat': 31.68, 'lng': 119.96}}]}),
            response({'status': 0, 'result': {'formatted_address': '江苏省常州市测试路'}}),
        ]):
            nearby = client.get('/api/places/around', params={
                'query': '公园', 'latitude': 31.6792, 'longitude': 119.9515,
            })
            address = client.get('/api/places/reverse', params={
                'latitude': 31.6792, 'longitude': 119.9515,
            })
        self.assertEqual(nearby.status_code, 200)
        self.assertEqual(nearby.json()['results'][0]['name'], '测试公园')
        self.assertEqual(address.status_code, 200)
        self.assertEqual(address.json(), {
            'address': '江苏省常州市测试路', 'place_name': '', 'nearby_results': [],
        })
        self.assertEqual(client.get('/api/places/around', params={
            'query': '公园', 'latitude': 91, 'longitude': 119.9515,
        }).status_code, 422)


if __name__ == '__main__':
    unittest.main()
