import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const frontendRoot = fileURLToPath(new URL('..', import.meta.url))
if (process.platform === 'win32') {
  const script = join(frontendRoot, 'scripts', 'copy-mapv-three-assets.ps1')
  const result = spawnSync('powershell.exe', [
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script,
  ], { stdio: 'inherit', windowsHide: true })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}

const packageRoot = join(frontendRoot, 'node_modules', '@baidumap', 'mapv-three')
const source = join(packageRoot, 'dist', 'assets')
const targetRoot = join(frontendRoot, 'public', 'mapvthree')
const target = join(targetRoot, 'assets')
const versionFile = join(targetRoot, '.copied-version')

if (!existsSync(source)) {
  throw new Error('找不到 mapv-three 的 dist/assets，请先执行 npm install。')
}

const version = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8')).version
if (existsSync(target) && existsSync(versionFile) && readFileSync(versionFile, 'utf8').trim() === version) {
  process.stdout.write(`MapV Three 静态资源已就绪（${version}）\n`)
} else {
  mkdirSync(targetRoot, { recursive: true })
  cpSync(source, target, { recursive: true, force: true })
  writeFileSync(versionFile, `${version}\n`)
  process.stdout.write(`已复制 MapV Three 静态资源到 public/mapvthree/assets（${version}）\n`)
}
