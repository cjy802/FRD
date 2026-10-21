export type RoadPreview = {
  geometry: { coordinates: number[][] }
  distance_m: number; distance_km: number; straight_distance_km: number; estimated_minutes: number
  altitude_m: number; road_names: string[]; start_snap_m: number; end_snap_m: number
  building_clearance_m: number | null; data_source: string
  risk: { level: string; score: number | null; reason: string }
  anchors?: Array<{ id: string; latitude: number; longitude: number }>
  requested_start?: { latitude: number; longitude: number }
  requested_end?: { latitude: number; longitude: number }
  snapped_start?: { latitude: number; longitude: number }
  snapped_end?: { latitude: number; longitude: number }
  anchor_indices?: number[]
  segments?: Array<{
    id: string; index: number; from_id: string; to_id: string
    start_index: number; end_index: number; distance_m: number; distance_km: number
    estimated_minutes: number; road_names: string[]; start_snap_m: number; end_snap_m: number
  }>
  area_coverage?: {
    road_classes: string[]
    target_road_m: number
    repeated_road_m: number
    connector_m: number
    coverage_ratio: number
    road_edge_count: number
    start_snap_m?: number
    end_snap_m?: number
    available_road_classes?: string[]
    route_closed?: boolean
  }
}
