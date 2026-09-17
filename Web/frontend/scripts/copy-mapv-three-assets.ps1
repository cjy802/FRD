$ErrorActionPreference = 'Stop'

$frontendRoot = Split-Path -Parent $PSScriptRoot
$packageRoot = Join-Path $frontendRoot 'node_modules\@baidumap\mapv-three'
$source = Join-Path $packageRoot 'dist\assets'
$targetRoot = Join-Path $frontendRoot 'public\mapvthree'
$target = Join-Path $targetRoot 'assets'
$versionFile = Join-Path $targetRoot '.copied-version'

if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw 'MapV Three dist/assets is missing. Run npm install first.'
}

$version = (Get-Content -LiteralPath (Join-Path $packageRoot 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
if ((Test-Path -LiteralPath $target -PathType Container) -and
    (Test-Path -LiteralPath $versionFile -PathType Leaf) -and
    ((Get-Content -LiteralPath $versionFile -Raw -Encoding Ascii).Trim() -eq $version)) {
    Write-Output "MapV Three assets are ready ($version)"
    exit 0
}

New-Item -ItemType Directory -Path $target -Force | Out-Null
Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse -Force
}
Set-Content -LiteralPath $versionFile -Value $version -Encoding Ascii
Write-Output "Copied MapV Three assets to public/mapvthree/assets ($version)"
