export type WeatherLimits = {
  name: string; max_wind_m_s: number; max_crosswind_m_s: number; max_headwind_m_s: number
  max_gust_m_s: number; max_rain_6h_mm: number; min_visibility_m: number
}
export type WeatherAlert = {
  field: string; reason: string; point_index: number; coordinate: number[]; eta_utc: string; level: string
}
export type WeatherRisk = {
  level: string; label: string; hazard_level: string; score_0_100: number | null
  score_definition: string; rule_mode: string; limits: WeatherLimits; coverage_complete: boolean
  alerts: WeatherAlert[]; triggered_point_count: number; limitations: string[]
  segments: { index: number; start: number[]; end: number[]; covered: boolean; level: string; score_0_100: number | null }[]
  summary: { max_wind_m_s: number | null; max_crosswind_m_s: number | null; max_headwind_m_s: number | null
    max_tailwind_m_s: number | null; max_surface_gust_m_s: number | null; min_surface_visibility_m: number | null; max_rain_6h_mm: number | null }
}
export type RouteAssessment = {
  state: string; departure_time_utc: string; arrival_time_utc: string
  sample_count: number; duration_seconds: number; message: string; eta_warning: string
  samples: { index: number; coordinate: number[]; eta_utc: string
    weather: { values: { t2m_K: number; tp_6h_mm: number }; derived: { wind_speed_10m_m_s: number }
      time_method: string; rain_interval: { start_utc: string; end_utc: string } }
    height_weather: { wind_speed_m_s: number; height_method: string; gust_surface_m_s: number; visibility_surface_m: number } | null }[]
  missing: { index: number; reason: string }[]; height_missing: { index: number; reason: string }[]
  coverage: { surface_space_time: boolean; flight_height: boolean }
  weather_risk: WeatherRisk
}
