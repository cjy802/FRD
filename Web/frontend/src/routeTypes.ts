export type RoadPreview = {
  geometry: { coordinates: number[][] }
  distance_m: number; distance_km: number; straight_distance_km: number; estimated_minutes: number
  altitude_m: number; road_names: string[]; start_snap_m: number; end_snap_m: number
  building_clearance_m: number | null; data_source: string
  risk: { level: string; score: number | null; reason: string }
}
