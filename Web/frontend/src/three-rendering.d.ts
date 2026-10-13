declare module 'three' {
  export const DoubleSide: number

  export class Vector3 {
    constructor(x?: number, y?: number, z?: number)
    x: number
    y: number
    z: number
    clone(): Vector3
    fromArray(values: number[]): this
    sub(other: Vector3): this
    subVectors(a: Vector3, b: Vector3): this
    cross(other: Vector3): this
    length(): number
    add(other: Vector3): this
    toArray(): number[]
    copy(other: Vector3): this
  }

  export class Float32BufferAttribute {
    constructor(values: number[], itemSize: number)
    count: number
    getX(index: number): number
    getY(index: number): number
    getZ(index: number): number
    fromBufferAttribute(attribute: Float32BufferAttribute, index: number): this
  }

  export class BufferGeometry {
    attributes: Record<string, Float32BufferAttribute>
    index: { count: number; getX(index: number): number } | null
    setAttribute(name: string, attribute: Float32BufferAttribute): this
    setIndex(indices: number[]): this
    computeBoundingSphere(): void
    dispose(): void
    addEventListener(type: 'dispose', callback: () => void): void
  }

  export class MeshBasicMaterial {
    constructor(options: {
      color: string
      opacity: number
      transparent: boolean
      side: number
      depthWrite: boolean
      depthTest: boolean
      toneMapped: boolean
      forceSinglePass: boolean
    })
    transparent: boolean
    side: number
    depthWrite: boolean
    depthTest: boolean
    color: { getHexString(): string }
    dispose(): void
    addEventListener(type: 'dispose', callback: () => void): void
  }

  export class Mesh<Geometry extends BufferGeometry = BufferGeometry, Material extends MeshBasicMaterial = MeshBasicMaterial> {
    constructor(geometry: Geometry, material: Material)
    geometry: Geometry
    material: Material
    position: Vector3
    name: string
    renderOrder: number
    raycast: (...args: unknown[]) => void
  }
}
