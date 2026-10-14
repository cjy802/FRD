[CmdletBinding()]
param(
    [ValidateSet('list', 'success', 'restore')]
    [string]$Scenario,
    [string]$FixtureRoot
)

$ErrorActionPreference = 'Stop'
$uploadScript = Join-Path (Split-Path $PSScriptRoot -Parent) 'rc2_mtp_upload.ps1'

if ($Scenario) {
    # This branch runs in a child PowerShell process. It replaces Shell.Application
    # with local folders, so the upload script never touches an MTP device.
    function New-MockItem {
        param([string]$Path)
        $item = [PSCustomObject]@{
            Name = Split-Path $Path -Leaf
            Path = $Path
            IsFolder = (Test-Path -LiteralPath $Path -PathType Container)
            Type = 'mock'
        }
        $item | Add-Member ScriptProperty GetFolder {
            if (-not $this.IsFolder) { return $null }
            return (New-MockFolder $this.Path)
        }
        return $item
    }
    function New-MockFolder {
        param([string]$Path)
        $folder = [PSCustomObject]@{ Path = $Path }
        $folder | Add-Member ScriptMethod Items {
            return @(Get-ChildItem -LiteralPath $this.Path | ForEach-Object { New-MockItem $_.FullName })
        }
        $folder | Add-Member ScriptMethod CopyHere {
            param($Item, $Flags)
            $source = if ($Item -is [string]) { $Item } else { $Item.Path }
            Copy-Item -LiteralPath $source -Destination $this.Path -Force
        }
        return $folder
    }
    function New-Object {
        param([string]$ComObject)
        if ($ComObject -ne 'Shell.Application') { throw "Unexpected New-Object: $ComObject" }
        return $global:mockShell
    }

    $global:mockShell = [PSCustomObject]@{ PcRoot = $FixtureRoot }
    $global:mockShell | Add-Member ScriptMethod Namespace {
        param($Path)
        if ($Path -eq 17) { return (New-MockFolder $this.PcRoot) }
        return (New-MockFolder $Path)
    }

    if ($Scenario -eq 'list') {
        & $uploadScript -ListOnly -Json
    } else {
        $uploadArgs = @{
            SourceKmz = Join-Path $FixtureRoot 'source.kmz'
            RemoteFileName = 'route.kmz'
            ReplaceExisting = $true
            Json = $true
            TimeoutSeconds = 1
            BackupRoot = Join-Path $FixtureRoot 'backups'
            SilentHelperPath = Join-Path $FixtureRoot 'mock-helper.ps1'
        }
        & $uploadScript @uploadArgs
    }
    return
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function New-Kmz {
    param([string]$Path, [string]$Value)
    $zip = [IO.Compression.ZipFile]::Open($Path, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($name in @('wpmz/template.kml', 'wpmz/waylines.wpml')) {
            $entry = $zip.CreateEntry($name)
            $writer = [IO.StreamWriter]::new($entry.Open())
            try { $writer.Write($Value) } finally { $writer.Dispose() }
        }
    } finally { $zip.Dispose() }
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Invoke-Scenario {
    param([string]$Name)
    $root = Join-Path ([IO.Path]::GetTempPath()) ("rc2-mtp-test-" + [guid]::NewGuid().ToString('N'))
    $targetDir = Join-Path $root 'DJI RC 2\Internal Shared Storage\Android\data\dji.go.v5\files\waypoint'
    $source = Join-Path $root 'source.kmz'
    $remote = Join-Path $targetDir 'route.kmz'
    try {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        New-Kmz $source 'new route'
        New-Kmz $remote 'old route'
        Set-Content -LiteralPath (Join-Path $targetDir 'ignore.txt') -Value 'ignored'
        New-Item -ItemType Directory -Path (Join-Path $targetDir 'folder.kmz') | Out-Null
        $oldHash = (Get-FileHash -LiteralPath $remote -Algorithm SHA256).Hash
        $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash

        $helper = Join-Path $root 'mock-helper.ps1'
        $helperPrelude = @(
            'param($Source, $TargetFolder, $RemoteName, $TargetFile)',
            '$fixture = [IO.Path]::GetFullPath((Split-Path $PSCommandPath -Parent)).TrimEnd("\", "/")',
            '$target = [IO.Path]::GetFullPath($TargetFile)',
            'if (-not $target.StartsWith($fixture + "\", [StringComparison]::OrdinalIgnoreCase) -or',
            '    (Split-Path $target -Leaf) -ne "route.kmz" -or $RemoteName -ne "route.kmz") { throw "Unsafe mock target" }'
        )
        if ($Name -eq 'restore') {
            $lines = $helperPrelude + @(
                '$marker = Join-Path $TargetFolder "first-run.marker"',
                'if (-not (Test-Path -LiteralPath $marker)) {',
                '  [IO.File]::WriteAllText($marker, "")',
                '  Remove-Item -LiteralPath $target -Force',
                '  $global:LASTEXITCODE = 3',
                '  return',
                '}',
                'Copy-Item -LiteralPath $Source -Destination $target -Force',
                '$global:LASTEXITCODE = 0'
            )
        } else {
            $lines = $helperPrelude + @(
                'Copy-Item -LiteralPath $Source -Destination $target -Force',
                '$global:LASTEXITCODE = 0'
            )
        }
        if ($Name -ne 'list') {
            [IO.File]::WriteAllLines($helper, $lines, [Text.Encoding]::ASCII)
        }

        $output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Scenario $Name -FixtureRoot $root 2>&1)
        $jsonLines = @($output | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith('{') })
        Assert-True ($jsonLines.Count -eq 1) "$Name did not emit exactly one JSON result: $($output -join ' | ')"
        $result = $jsonLines[0] | ConvertFrom-Json

        if ($Name -eq 'list') {
            Assert-True ($result.status -eq 'success' -and $result.stage -eq 'list') 'List failed'
            Assert-True ($result.targets.Count -eq 1 -and $result.targets[0].name -eq 'route.kmz') 'List included a non-KMZ item or folder'
        } elseif ($Name -eq 'success') {
            Assert-True ($result.status -eq 'success' -and $result.remote_state -eq 'verified') 'Upload was not verified'
            Assert-True ($result.source_sha256 -eq $sourceHash -and $result.readback_sha256 -eq $sourceHash) 'Upload hashes differ'
            Assert-True ((Get-FileHash -LiteralPath $remote -Algorithm SHA256).Hash -eq $sourceHash) 'Remote file is not the new KMZ'
            Assert-True ((Test-Path -LiteralPath $result.backup_path -PathType Leaf) -and
                (Get-FileHash -LiteralPath $result.backup_path -Algorithm SHA256).Hash -eq $oldHash) 'Backup differs from the old KMZ'
        } else {
            Assert-True ($result.status -eq 'error' -and $result.failure_stage -eq 'replacing') 'Injected helper failure was not reported'
            Assert-True ($result.restore_attempted -and $result.restore_verified -and $result.remote_state -eq 'restored') 'Old KMZ was not restored and verified'
            Assert-True ((Get-FileHash -LiteralPath $remote -Algorithm SHA256).Hash -eq $oldHash) 'Restored remote file differs from the old KMZ'
            Assert-True ((Test-Path -LiteralPath $result.backup_path -PathType Leaf) -and
                (Get-FileHash -LiteralPath $result.backup_path -Algorithm SHA256).Hash -eq $oldHash) 'Backup was not retained'
        }
        Write-Host "PASS $Name"
    } finally {
        # The only recursive delete is of this GUID-named directory created above.
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
        $resolved = [IO.Path]::GetFullPath($root)
        if ($resolved.StartsWith($tempRoot + '\', [StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path $resolved -Leaf) -match '^rc2-mtp-test-[0-9a-f]{32}$' -and
            (Test-Path -LiteralPath $resolved -PathType Container)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}

foreach ($name in @('list', 'success', 'restore')) {
    Invoke-Scenario $name
}
