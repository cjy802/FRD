import { BufferGeometry, DoubleSide, Float32BufferAttribute, Mesh, MeshBasicMaterial, Vector3 } from 'three'

export type Coordinate = number[]
export type CameraPose = { longitude: number; latitude: number; altitude: number; heading: number; pitch: number }
export type SceneView = '2d' | '3d'

let sdkPromise: Promise<any> | null = null
async function loadSdk() {
  const ak = import.meta.env.VITE_BAIDU_MAP_AK?.trim()
  if (!ak) throw new Error('尚未配置百度地图 AK。')
  ;(window as any).MAPV_BASE_URL = `${import.meta.env.BASE_URL}mapvthree/`
  sdkPromise ??= import('@baidumap/mapv-three').catch(error => { sdkPromise = null; throw error })
  const sdk = await sdkPromise
  sdk.BaiduMapConfig.ak = ak
  return { sdk, ak }
}

// Geographic coordinates are WGS84. The provider reprojects Baidu imagery;
// applying a second BD09 offset would misalign routes and camera positions.
export async function createBaiduScene(element: HTMLElement, center: Coordinate, preview = false) {
  const { sdk, ak } = await loadSdk()
  const engine: any = new sdk.Engine(element, {
    map: {
      // The raster surface reprojects the satellite tiles onto the globe.
      provider: new sdk.Baidu09ImageryTileProvider({ ak, type: 'satellite' }),
      projection: 'ecef', center, heading: 0, pitch: 52, range: 900,
    },
    controller: { enabled: !preview, enableRotate: !preview, enableZoom: !preview, enablePan: !preview, enableTilt: !preview,
      inertiaTranslate: 0, inertiaZoom: 0, enableTerrainCollision: false },
    rendering: { enableAnimationLoop: true },
  } as any)
  if (preview) engine.map.fov = 58
  return new BaiduScene(engine, sdk, element, !preview)
}

export class BaiduScene {
  private routeLayers: any[] = []
  private viewLayers: any[] = []
  private viewMeshes: Mesh<BufferGeometry, MeshBasicMaterial>[] = []
  private frameListeners = new Set<() => void>()
  private disposed = false
  private beforeRender = () => { for (const callback of this.frameListeners) callback() }

  constructor(readonly engine: any, private sdk: any, private element: HTMLElement, private interactive = true) {
    engine.addBeforeRenderListener(this.beforeRender)
  }

  private clear(layers: any[]) {
    // Removing from the engine also disposes each layer's render resources.
    for (const layer of layers.splice(0)) this.engine.remove(layer)
  }

  private geometry(type: string, coordinates: any) {
    return this.sdk.GeoJSONDataSource.fromGeoJSON({ type: 'FeatureCollection', features: [
      { type: 'Feature', properties: {}, geometry: { type, coordinates } },
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
      this.engine.map.projectArrayCoordinate(coordinate, position)
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
    this.engine.map.setProjection(view === '3d' ? 'ecef' : 'EPSG:3857')
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
    this.engine.map.lookAt(center, { heading: 0, pitch: view === '2d' ? 0 : 52, range: Math.max(650, span * 2.5 * aspectFactor, height * 6) })
    this.engine.requestRender()
  }

  focus(coordinate: Coordinate, view: SceneView) {
    this.engine.map.lookAt([coordinate[0], coordinate[1], 0], {
      heading: view === '2d' ? 0 : this.engine.map.getHeading(),
      pitch: view === '2d' ? 0 : this.engine.map.getPitch(),
      range: Math.max(550, (coordinate[2] || 0) * 5),
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
    earth.setView([pose.longitude, pose.latitude, pose.altitude], {
      heading: ((360 - pose.heading) % 360 + 360) % 360, pitch: 90 + pose.pitch, roll: 0,
    })
    this.engine.requestRender()
  }

  project(coordinate: Coordinate) {
    const camera = this.engine.camera
    const projected: number[] = []
    this.engine.map.projectArrayCoordinate(coordinate, projected)
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
    return geographic && Math.abs(geographic[0]) <= 180 && Math.abs(geographic[1]) <= 90 ? geographic : null
  }

  onFrame(callback: () => void) { this.frameListeners.add(callback); return () => this.frameListeners.delete(callback) }
  setInputs(enabled: boolean) {
    this.engine.controller.enabled = enabled && this.interactive
    const controller = this.engine.map._map?.controller
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
    this.clear(this.viewLayers)
    this.clearViewMeshes()
    this.engine.dispose()
  }
}
