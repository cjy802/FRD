param(
    [switch]$Run,
    [int]$MaxRuns = 1,
    [string[]]$Dates = @()
)

$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $appRoot
$aliasRoot = 'D:\DATASET\FuXi_RuntimeLinks'
$aliasSpecs = @(
    @{ Name = 'FuXiCodeData'; Target = (Join-Path $projectRoot 'FuXi\FuXi-main') },
    @{ Name = 'FuXiApp'; Target = $appRoot },
    @{ Name = 'FuXiEC'; Target = (Join-Path $projectRoot 'FuXi_EC') }
)

New-Item -ItemType Directory -Path $aliasRoot -Force | Out-Null
foreach ($spec in $aliasSpecs) {
    $aliasPath = Join-Path $aliasRoot $spec.Name
    if (-not (Test-Path -LiteralPath $spec.Target -PathType Container)) {
        throw "Junction target does not exist: $($spec.Target)"
    }
    if (Test-Path -LiteralPath $aliasPath) {
        $existing = Get-Item -LiteralPath $aliasPath -Force
        $targetValues = @(
            $existing.Target | Where-Object {
                -not [string]::IsNullOrWhiteSpace([string]$_)
            }
        )
        if ($existing.LinkType -ne 'Junction' -or $targetValues.Count -ne 1) {
            throw "Existing path is not the expected junction: $aliasPath"
        }
        $actualTarget = [System.IO.Path]::GetFullPath([string]$targetValues[0]).TrimEnd('\')
        $expectedTarget = [System.IO.Path]::GetFullPath([string]$spec.Target).TrimEnd('\')
        if ($actualTarget -ne $expectedTarget) {
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

$code = Join-Path $aliasRoot 'FuXiCodeData'
$app = Join-Path $aliasRoot 'FuXiApp'
$model = Join-Path $aliasRoot 'FuXiEC'
$batchArgs = @(
    (Join-Path $PSScriptRoot 'build_fuxi_pairs.py'),
    '--pressure-dir', 'D:\DATASET\FuXi_ERA5\pressure',
    '--surface', (Join-Path $code '535c7c7a1149e12c69a765f71f93a242\data_stream-oper_stepType-instant.nc'),
    '--wind', (Join-Path $code '1cb39f095b03722d7030da272927ff99\data_stream-oper_stepType-instant.nc'),
    '--precip', (Join-Path $code '62bebd9af378805411e61a14abeaa385\data_stream-oper_stepType-accum.nc'),
    '--model-dir', $model,
    '--observations', (Join-Path $appRoot 'point_data\era5_surface_point_31p5N_119p5E_2026.csv'),
    '--prepared-dir', (Join-Path $app 'prepared'),
    '--output', (Join-Path $appRoot 'point_data\fuxi_era5_pairs_6h.csv')
)

foreach ($day in $Dates) {
    $batchArgs += @('--date', $day)
}
if ($Run) {
    $batchArgs += @('--run', '--max-runs', [string]$MaxRuns)
}

& $pythonExe @batchArgs
exit $LASTEXITCODE
