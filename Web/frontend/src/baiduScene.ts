import { fromBaiduSceneCoordinate, toBaiduSceneCoordinate } from './baiduCoordinates'
import { BufferGeometry, DoubleSide, Float32BufferAttribute, Mesh, MeshBasicMaterial, Vector3 } from 'three'

export type Coordinate = number[]
export type CameraPose = { longitude: number; latitude: number; altitude: number; heading: number; pitch: number }
export type SceneView = '2d' | '3d'
export type MapProvider = 'baidu' | 'osm'

let sdkPromise: Promise<any> | null = null
async function loadSdk(requireAk = true) {
  const ak = import.meta.env.VITE_BAIDU_MAP_AK?.trim()
  if (requireAk && !ak) throw new Error('尚未配置百度地图 AK。')
  ;(window as any).MAPV_BASE_URL = `${import.meta.env.BASE_URL}mapvthree/`
  sdkPromise ??= import('@baidumap/mapv-three').catch(error => { sdkPromise = null; throw error })
  const sdk = await sdkPromise
  if (ak) sdk.BaiduMapConfig.ak = ak
  return { sdk, ak }
}

// All public inputs/outputs are WGS84. Reprojecting the Baidu tiles changes
// their projection, but does not convert their BD-09 geographic datum. Convert
// once at each renderer boundary; keep mission data in WGS84.
export async function createMapScene(element: HTMLElement, center: Coordinate, provider: MapProvider = 'baidu', preview = false) {
  const { sdk, ak } = await loadSdk(provider === 'baidu')
  const geoDatum = provider === 'baidu' ? 'bd09' : 'wgs84'
  const toScene = (coordinate: Coordinate) => geoDatum === 'bd09' ? toBaiduSceneCoordinate(coordinate) : [...coordinate]
  const engine: any = new sdk.Engine(element, {
    map: {
      provider: provider === 'baidu'
        ? new sdk.Baidu09ImageryTileProvider({ ak, type: 'satellite' })
        : new sdk.OSMImageryTileProvider(),
      projection: provider === 'baidu' ? 'ecef' : 'EPSG:3857', center: toScene(center), heading: 0, pitch: 52, range: 900,
    },
    controller: { enabled: !preview, enableRotate: !preview, enableZoom: !preview, enablePan: !preview, enableTilt: !preview,
      inertiaTranslate: 0, inertiaZoom: 0, enableTerrainCollision: false },
    rendering: { enableAnimationLoop: true },
  } as any)
  if (preview) engine.map.fov = 58
  const scene = new BaiduScene(engine, sdk, element, !preview, geoDatum)
  scene.setInputs(!preview)
  return scene
}

export async function createBaiduScene(element: HTMLElement, center: Coordinate, preview = false) {
  return createMapScene(element, center, 'baidu', preview)
}

export class BaiduScene {
  private routeLayers: any[] = []
  private telemetryLayers: any[] = []
  private viewLayers: any[] = []
  private viewMeshes: Mesh<BufferGeometry, MeshBasicMaterial>[] = []
  private frameListeners = new Set<() => void>()
  private disposed = false
  private beforeRender = () => { for (const callback of this.frameListeners) callback() }

  constructor(readonly engine: any, private sdk: any, private element: HTMLElement, private interactive = true, private geoDatum: 'bd09' | 'wgs84' = 'bd09') {
    engine.addBeforeRenderListener(this.beforeRender)
  }

  private clear(layers: any[]) {
    // Removing from the engine also disposes each layer's render resources.
    for (const layer of layers.splice(0)) this.engine.remove(layer)
  }

  private geometry(type: string, coordinates: any) {
    const toScene = (value: Coordinate) => this.geoDatum === 'bd09' ? toBaiduSceneCoordinate(value) : [...value]
    const convert = (value: any): any => typeof value[0] === 'number'
      ? toScene(value)
      : value.map(convert)
    return this.sdk.GeoJSONDataSource.fromGeoJSON({ type: 'FeatureCollection', features: [
      { type: 'Feature', properties: {}, geometry: { type, coordinates: convert(coordinates) } },
    ] })
  }

  private line(layers: any[], coordinates: Coordinate[], color: string, width: number, opacity = 1) {
    if (coordinates.length < 2) return
    const layer = new this.sdk.Polyline({ flat: false, color, lineWidth: width, opacity })
    this.engine.add(layer)
    layers.push(layer)
    // GeoObject.setDataSource reads its engine's projection immediately.
    layer.dataSource = this.geometry('LineString', coordinates)
  }

  private polygon(layers: any[], coordinates: Coordinate[], color: string, opacity: number) {
    if (coordinates.length < 3) return
    const layer = new this.sdk.Polygon({ color, opacity, transparent: true, side: DoubleSide, depthWrite: false, perPositionHeight: true })
    this.engine.add(layer)
    layers.push(layer)
    layer.dataSource = this.geometry('Polygon', [[...coordinates, coordinates[0]]])
  }

  private clearViewMeshes() {
    // Plain Three meshes have no SDK disposal hook.
    for (const mesh of this.viewMeshes.splice(0)) {
      this.engine.remove(mesh)
      mesh.geometry.dispose()
      mesh.material.dispose()
    }
  }

  private viewFace(coordinates: Coordinate[], opacity: number) {
    const projected = coordinates.map(coordinate => {
      const position: number[] = []
      const sceneCoordinate = this.geoDatum === 'bd09' ? toBaiduSceneCoordinate(coordinate) : coordinate
      this.engine.map.projectArrayCoordinate(sceneCoordinate, position)
      return new Vector3().fromArray(position)
    })
    // Store small local coordinates to retain precision on the ECEF globe.
    const center = projected[0].clone()
    const geometry = new BufferGeometry()
    geometry.setAttribute('position', new Float32BufferAttribute(projected.flatMap(position => position.sub(center).toArray()), 3))
    geometry.setIndex(coordinates.length === 3 ? [0, 1, 2] : [0, 1, 2, 0, 2, 3])
    geometry.computeBoundingSphere()
    const material = new MeshBasicMaterial({ color: '#36dfba', opacity, transparent: true,
      side: DoubleSide, depthWrite: false, depthTest: true, toneMapped: false, forceSinglePass: true })
    const mesh = new Mesh(geometry, material)
    mesh.name = 'camera-view-face'
    mesh.position.copy(center)
    mesh.renderOrder = -1
    // Filled faces must not intercept map picking or camera dragging.
    mesh.raycast = () => {}
    this.engine.add(mesh)
    this.viewMeshes.push(mesh)
  }

  setRoute(route: Coordinate[], view: SceneView, weather: { start: number[]; end: number[]; level: string }[], ranges: Coordinate[][]) {
    this.clear(this.routeLayers)
    const ground = route.map(([lng, lat]) => [lng, lat, 1])
    const positions = view === '2d' ? route.map(([lng, lat]) => [lng, lat, 3]) : route
    if (view === '3d') {
      this.line(this.routeLayers, ground, '#082b2a', 3, .7)
      for (let i = 0; i < route.length; i++) this.line(this.routeLayers, [ground[i], route[i]], '#e8fff8', 1, .65)
    }
    // Put the outline below the green line. Coplanar wide polylines compete
    // for the same depth pixels and expose dark/colored fragments instead.
    this.line(this.routeLayers, positions.map(([lng, lat, height]) => [lng, lat, height - .5]), '#082b2a', 9, .8)
    this.line(this.routeLayers, positions, '#00e5a8', 4)
    const colors: Record<string, string> = { no_trigger: '#0f6cbd', attention: '#b66a00', elevated: '#b42318', incomplete: '#6e7781' }
    for (const segment of weather) {
      // Weather follows the ground trace. In 2D, use a wider halo below the
      // outline/core so risk colors stay visible without replacing the route.
      this.line(this.routeLayers, [[...segment.start.slice(0, 2), 2], [...segment.end.slice(0, 2), 2]], colors[segment.level] ?? '#6e7781', view === '2d' ? 13 : 5)
    }
    ranges.forEach((ring, index) => {
      this.polygon(this.routeLayers, ring, index ? '#5b5fc7' : '#0f6cbd', .12)
      this.line(this.routeLayers, [...ring, ring[0]], index ? '#5b5fc7' : '#0f6cbd', 1)
    })
    this.engine.requestRender()
  }

  setTelemetryTrack(track: Coordinate[]) {
    this.clear(this.telemetryLayers)
    const groundTrack = track
      .filter(point => point.length >= 2 && Number.isFinite(point[0]) && Number.isFinite(point[1]))
      .map(([longitude, latitude]) => [longitude, latitude, 2])
    if (groundTrack.length > 1) this.line(this.telemetryLayers, groundTrack, '#d97706', 4, .95)
    this.engine.requestRender()
  }

  setFootprint(origin: Coordinate | null, footprint: Coordinate[], view: SceneView) {
    this.clear(this.viewLayers)
    this.clearViewMeshes()
    if (!origin || footprint.length < 3) { this.engine.requestRender(); return }
    const corners = footprint.map(([lng, lat, height = 2]) => [lng, lat, view === '2d' ? 2 : Math.max(2, height)])
    if (view === '2d') this.polygon(this.viewLayers, corners, '#36dfba', .3)
    else this.viewFace(corners, .3)
    this.line(this.viewLayers, [...corners, corners[0]], '#26e7b6', 2)
    const eye = view === '2d' ? [origin[0], origin[1], 2] : origin
    corners.forEach((corner, index) => {
      this.line(this.viewLayers, [eye, corner], '#ffb547', 1.5, .8)
      if (view === '3d') this.viewFace([eye, corner, corners[(index + 1) % corners.length]], .22)
    })
    this.engine.requestRender()
  }

  setView(view: SceneView, coordinates: Coordinate[]) {
    // Both OSM/WGS84 and Baidu/BD-09 scenes need the same projection switch.
    // Keeping WGS84 in EPSG:3857 made the 3D control change state without
    // changing the actual map projection, which was especially visible in
    // area coverage mode where the OSM map is used by default.
    const projection = view === '3d' ? 'ecef' : 'EPSG:3857'
    this.engine.map.setProjection(projection)
    this.engine.controller.enableTilt = view === '3d'
    this.engine.controller.enableRotate = view === '3d'
    this.fit(coordinates, view)
  }

  fit(coordinates: Coordinate[], view: SceneView) {
    if (!coordinates.length) return
    const lngs = coordinates.map(point => point[0]), lats = coordinates.map(point => point[1])
    const center = [(Math.min(...lngs) + Math.max(...lngs)) / 2, (Math.min(...lats) + Math.max(...lats)) / 2, 0]
    const span = Math.max((Math.max(...lngs) - Math.min(...lngs)) * 111320 * Math.cos(center[1] * Math.PI / 180), (Math.max(...lats) - Math.min(...lats)) * 111320)
    const height = view === '3d' ? Math.max(...coordinates.map(point => point[2] || 0)) : 0
    const bounds = this.element.getBoundingClientRect()
    const aspectFactor = Math.max(1, bounds.height / Math.max(1, bounds.width))
    const sceneCenter = this.geoDatum === 'bd09' ? toBaiduSceneCoordinate(center) : center
    this.engine.map.lookAt(sceneCenter, { heading: 0, pitch: view === '2d' ? 0 : 52, range: Math.max(650, span * 2.5 * aspectFactor, height * 6) })
    this.engine.requestRender()
  }

  focus(coordinate: Coordinate, view: SceneView) {
    const sceneCoordinate = this.geoDatum === 'bd09' ? toBaiduSceneCoordinate([coordinate[0], coordinate[1], 0]) : [coordinate[0], coordinate[1], 0]
    this.engine.map.lookAt(sceneCoordinate, {
      heading: view === '2d' ? 0 : this.engine.map.getHeading(),
      pitch: view === '2d' ? 0 : this.engine.map.getPitch(),
      range: Math.max(550, (coordinate[2] || 0) * 5),
    })
    this.engine.requestRender()
  }

  followAt(pose: CameraPose, view: SceneView) {
    const sceneCoordinate = this.geoDatum === 'bd09'
      ? toBaiduSceneCoordinate([pose.longitude, pose.latitude, pose.altitude])
      : [pose.longitude, pose.latitude, pose.altitude]
    const currentRange = this.engine.map.getRange()
    const currentPitch = this.engine.map.getPitch()
    const pitch = view === '2d' ? 0 : Number.isFinite(currentPitch) ? currentPitch : 52
    // getRange() reaches the ground under the view center while lookAt measures
    // to the elevated target; re-feeding the longer ground distance grows the
    // range every frame and the camera climbs away from the drone.
    const altitudeLeg = pose.altitude / Math.max(.05, Math.cos(pitch * Math.PI / 180))
    // Map headings turn counterclockwise; mission headings turn clockwise.
    // Keep the observer's distance and tilt while the target tracks the drone.
    this.engine.map.lookAt(sceneCoordinate, {
      heading: ((360 - pose.heading) % 360 + 360) % 360,
      pitch,
      range: Number.isFinite(currentRange) && currentRange > 0 ? Math.max(50, currentRange - altitudeLeg) : 900,
    })
    this.engine.requestRender()
  }

  setTilt(degrees: number) { this.engine.map.setPitch(degrees); this.engine.requestRender() }

  getViewAngles(): { heading: number; pitch: number } {
    return { heading: this.engine.map.getHeading(), pitch: this.engine.map.getPitch() }
  }

  setViewAngles(heading: number, pitch: number) {
    this.engine.map.setHeading(heading)
    this.engine.map.setPitch(pitch)
    this.engine.requestRender()
  }

  setPose(pose: CameraPose) {
    // JSAPI Three 1.7.2 exposes the eye-based setView on its Earth map. Keep
    // that version-specific access here: orbiting with lookAt(range: 0) produces
    // an undefined direction, and a positive range would move the camera.
    const earth = this.engine.map._map
    if (typeof earth?.setView !== 'function') throw new Error('当前百度引擎不支持固定位置的相机视角。')
    const sceneCoordinate = this.geoDatum === 'bd09' ? toBaiduSceneCoordinate([pose.longitude, pose.latitude, pose.altitude]) : [pose.longitude, pose.latitude, pose.altitude]
    earth.setView(sceneCoordinate, {
      heading: ((360 - pose.heading) % 360 + 360) % 360, pitch: 90 + pose.pitch, roll: 0,
    })
    this.engine.requestRender()
  }

  project(coordinate: Coordinate) {
    // Before-render callbacks run after the SDK temporarily shifts the scene
    // origin for precision. Use its absolute camera for geographic positions.
    const camera = this.engine.rendering.rawCamera ?? this.engine.camera
    const projected: number[] = []
    const sceneCoordinate = this.geoDatum === 'bd09' ? toBaiduSceneCoordinate(coordinate) : coordinate
    this.engine.map.projectArrayCoordinate(sceneCoordinate, projected)
    const world = camera.position.clone().fromArray(projected)
    if (this.engine.map.isGlobe) {
      // Hide back-side nodes even when their imagery has not loaded yet.
      const a = 6378137, b = 6356752.314245
      const eye = camera.position.clone().set(camera.position.x / a, camera.position.y / a, camera.position.z / b)
      const target = world.clone().set(world.x / a, world.y / a, world.z / b)
      const delta = target.clone().sub(eye)
      const aa = delta.dot(delta), bb = 2 * eye.dot(delta), cc = eye.dot(eye) - 1
      const discriminant = bb * bb - 4 * aa * cc
      if (aa && discriminant >= 0) {
        const t = (-bb - Math.sqrt(discriminant)) / (2 * aa)
        if (t > 0 && t < 1 - 1e-7) return null
      }
    }
    const pixel = world.project(camera)
    if (![pixel.x, pixel.y, pixel.z].every(Number.isFinite) || pixel.z < -1 || pixel.z > 1 || Math.abs(pixel.x) > 1.05 || Math.abs(pixel.y) > 1.05) return null
    const bounds = this.element.getBoundingClientRect()
    return { x: (pixel.x + 1) * bounds.width / 2, y: (1 - pixel.y) * bounds.height / 2 }
  }

  pick(x: number, y: number): Coordinate | null {
    const point = this.engine.map.pickSeaLevelWorldPosition({ x, y })
    if (!point || ![point.x, point.y, point.z].every(Number.isFinite) || point.lengthSq() === 0) return null
    const geographic = this.engine.map.unprojectArrayCoordinate(point.toArray())
    const restored = this.geoDatum === 'bd09' ? fromBaiduSceneCoordinate(geographic) : geographic
    return geographic && Math.abs(restored[0]) <= 180 && Math.abs(restored[1]) <= 90 ? restored : null
  }

  onFrame(callback: () => void) { this.frameListeners.add(callback); return () => this.frameListeners.delete(callback) }
  setInputs(enabled: boolean) {
    this.engine.controller.enabled = enabled && this.interactive
    // Earth uses Cesium-style enableInputs; the planar controller uses enabled.
    // EngineController.enabled alone does not disable Earth's default inputs.
    const controller = this.engine.map._map.controller
    if (controller && 'enableInputs' in controller) controller.enableInputs = enabled && this.interactive
  }
  setActive(active: boolean) {
    this.setInputs(active)
    this.engine.rendering.enableAnimationLoop = active
    if (active) this.engine.requestRender()
  }
  resize() { this.engine.requestRender() }
  dispose() {
    if (this.disposed) return
    this.disposed = true
    this.engine.removeBeforeRenderListener(this.beforeRender)
    this.frameListeners.clear()
    this.clear(this.routeLayers)
    this.clear(this.telemetryLayers)
    this.clear(this.viewLayers)
    this.clearViewMeshes()
    this.engine.dispose()
  }
}
