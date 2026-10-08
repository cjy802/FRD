import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient

from main import app, road_candidates_cached
from road_planner import CampusRoadNetwork, METRES_PER_DEGREE_LAT, METRES_PER_DEGREE_LON, RouteUnavailable
from route_candidates import local_road_candidates
from weather_routes import compare_road_weather


class WeatherRouteTests(unittest.TestCase):
    def setUp(self):
        self.grid = SimpleNamespace(latitudes=np.array([32, 31.75, 31.5, 31.25]), longitudes=np.array([119, 119.25, 119.5, 119.75, 120]))

    def route(self, coordinates, distance=500):
        return {'geometry': {'type': 'LineString', 'coordinates': coordinates}, 'distance_m': distance,
                'straight_distance_m': 400, 'start_snap_m': 0, 'end_snap_m': 0, 'road_names': ['Mock road'],
                'building_clearance_m': 2, 'data_source': 'Mock roads'}

    def evaluate(self, routes, scores=(50, 20), winds=(13, 8), covered=True):
        def function(coordinates):
            index = next(i for i, route in enumerate(routes) if route['geometry']['coordinates'] == coordinates)
            return {'samples': [{'coordinate': p} for p in coordinates], 'missing': [],
                    'weather_risk': {'level': 'elevated' if index == 0 else 'no_trigger', 'label': 'Mock',
                                     'score_0_100': scores[index] if covered else None, 'coverage_complete': covered, 'rule_mode': 'demo',
                                     'summary': {'max_wind_m_s': winds[index], 'max_surface_gust_m_s': 10,
                                                 'max_rain_6h_mm': 0, 'min_surface_visibility_m': 10000}}}
        return function

    def test_same_native_cells_cannot_claim_weather_detour_improvement(self):
        routes = [self.route([[119.02, 31.9], [119.2, 31.9]]),
                  self.route([[119.02, 31.9], [119.1, 31.91], [119.2, 31.9]], 600)]
        result = compare_road_weather(routes, self.grid, self.evaluate(routes))
        self.assertEqual(result['state'], 'no_resolvable_weather_difference')
        self.assertEqual(result['recommended_route_id'], 'road_0')
        self.assertIsNone(result['flight_permission'])

    def test_only_covered_distinct_grid_significant_weather_lower_score_can_propose_candidate(self):
        routes = [self.route([[119.02, 31.9], [119.2, 31.9]]),
                  self.route([[119.02, 31.9], [119.1, 31.7], [119.2, 31.9]], 600)]
        result = compare_road_weather(routes, self.grid, self.evaluate(routes))
        self.assertEqual(result['state'], 'candidate_lower_screening')
        self.assertEqual(result['recommended_route_id'], 'road_1')
        unchanged = compare_road_weather(routes, self.grid, self.evaluate(routes, winds=(13, 13)))
        self.assertEqual(unchanged['state'], 'no_material_improvement')
        missing = compare_road_weather(routes, self.grid, self.evaluate(routes, covered=False))
        self.assertEqual(missing['state'], 'coverage_incomplete')
        routes[1]['distance_m'] = 1000
        excessive = compare_road_weather(routes, self.grid, self.evaluate(routes))
        self.assertEqual(excessive['recommended_route_id'], 'road_0')

    def test_local_alternative_uses_existing_loop_and_preserves_geometric_distance(self):
        lat, lon = 31.6798, 119.9505
        def coord(x, y): return (lon + x / METRES_PER_DEGREE_LON, lat + y / METRES_PER_DEGREE_LAT)
        nodes = {1: coord(0, 0), 2: coord(200, 0), 3: coord(0, 20), 4: coord(200, 20)}
        roads = [(i, a, b, *nodes[a], *nodes[b], i, f'Road {i}', 'service') for i, (a, b) in enumerate(((1, 2), (1, 3), (3, 4), (4, 2)), 1)]
        network = CampusRoadNetwork.from_index_rows(roads, [], lat, lon, 'Mock loop')
        routes = local_road_candidates(network, nodes[1][1], nodes[1][0], nodes[2][1], nodes[2][0], 0)
        self.assertEqual(len(routes), 2)
        self.assertNotEqual(routes[0]['geometry'], routes[1]['geometry'])
        self.assertAlmostEqual(routes[0]['distance_m'], 200, delta=.5)
        self.assertLess(routes[1]['distance_m'], 400)
        self.assertTrue(all(route['start_snap_m'] <= 35 and route['end_snap_m'] <= 35 for route in routes))
        with self.assertRaises(RouteUnavailable): network.plan(lat, lon, nodes[2][1], nodes[2][0], 0, edge_penalty=lambda a, b: -1)

    def test_comparison_api_formats_candidates_and_unknown_job_does_not_route(self):
        routes = [self.route([[119.9515, 31.6792], [119.9495, 31.6804]])]
        manager = Mock()
        manager.regional_job.return_value = (self.grid, None, '2026-10-08T01:00:00Z')
        manager.route.side_effect = lambda job_id, coords, *args: self.evaluate(routes, scores=(10,), winds=(3,))(coords)
        payload = {'weather_job_id': 'test', 'start': {'latitude': 31.6792, 'longitude': 119.9515},
                   'end': {'latitude': 31.6804, 'longitude': 119.9495}, 'departure_time_utc': '2026-10-08T03:00:00Z'}
        with patch('main.forecast_jobs', manager), patch('main.road_candidates_cached', return_value=routes) as planner:
            client = TestClient(app)
            response = client.post('/api/routes/compare-weather', json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertIn('distance_km', response.json()['candidates'][0]['route'])
            self.assertEqual(response.json()['state'], 'no_alternative')
            manager.regional_job.side_effect = KeyError('unknown')
            self.assertEqual(client.post('/api/routes/compare-weather', json=payload).status_code, 404)
            self.assertEqual(planner.call_count, 1)


if __name__ == '__main__': unittest.main()
