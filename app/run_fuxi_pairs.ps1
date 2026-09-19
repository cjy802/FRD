param(
    [switch]$Run,
    [int]$MaxRuns = 1,
    [string[]]$Dates = @()
)

$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $appRoot
$aliasSpecs = @(
    @{ Name = 'FuXiCodeData'; Target = (Join-Path $projectRoot 'FuXi\FuXi-main') },
    @{ Name = 'FuXiApp'; Target = $appRoot },
    @{ Name = 'FuXiEC'; Target = (Join-Path $projectRoot 'FuXi_EC') }
)

foreach ($spec in $aliasSpecs) {
    $aliasPath = Join-Path $env:TEMP $spec.Name
    if (-not (Test-Path -LiteralPath $spec.Target -PathType Container)) {
        throw "Junction target does not exist: $($spec.Target)"
    }
    if (Test-Path -LiteralPath $aliasPath) {
        $existing = Get-Item -LiteralPath $aliasPath -Force
        $actualTarget = [System.IO.Path]::GetFullPath([string]$existing.Target).TrimEnd('\')
        $expectedTarget = [System.IO.Path]::GetFullPath([string]$spec.Target).TrimEnd('\')
        if ($existing.LinkType -ne 'Junction' -or $actualTarget -ne $expectedTarget) {
            throw "Existing path is not the expected junction: $aliasPath"
        }
    } else {
        New-Item -ItemType Junction -Path $aliasPath -Target $spec.Target | Out-Null
    }
}

$pythonExe = 'D:\anaconda\envs\fuxi_onnx\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python environment not found: $pythonExe"
}

$code = Join-Path $env:TEMP 'FuXiCodeData'
$app = Join-Path $env:TEMP 'FuXiApp'
$model = Join-Path $env:TEMP 'FuXiEC'
$batchArgs = @(
    (Join-Path $PSScriptRoot 'build_fuxi_pairs.py'),
    '--pressure-dir', 'D:\DATASET\FuXi_ERA5\pressure',
    '--surface', (Join-Path $code '535c7c7a1149e12c69a765f71f93a242\data_stream-oper_stepType-instant.nc'),
    '--wind', (Join-Path $code '1cb39f095b03722d7030da272927ff99\data_stream-oper_stepType-instant.nc'),
    '--precip', (Join-Path $code '62bebd9af378805411e61a14abeaa385\data_stream-oper_stepType-accum.nc'),
    '--model-dir', $model,
    '--observations', (Join-Path $app 'point_data\era5_surface_point_31p5N_119p5E_2026.csv'),
    '--prepared-dir', (Join-Path $app 'prepared'),
    '--output', (Join-Path $app 'point_data\fuxi_era5_pairs_6h.csv')
)

foreach ($day in $Dates) {
    $batchArgs += @('--date', $day)
}
if ($Run) {
    $batchArgs += @('--run', '--max-runs', [string]$MaxRuns)
}

& $pythonExe @batchArgs
exit $LASTEXITCODE
