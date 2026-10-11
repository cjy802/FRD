[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$SourceKmz,
    [string]$DeviceName = 'DJI RC 2',
    [string]$RemoteFileName,
    [string]$WaypointPath = 'Android/data/dji.go.v5/files/waypoint',
    [string]$BackupRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'DjiRc2MtpBackups'),
    [int]$TimeoutSeconds = 60,
    [switch]$ReplaceExisting,
    [switch]$DryRun
)

# MTP devices do not have a drive letter. This prototype uses Shell/WPD.
# It stages a unique remote file, reads it back and checks SHA-256, then writes
# the requested name. WPD Shell has no reliable atomic rename; replacement has
# a short gap, so the old file is backed up before deletion.
$ErrorActionPreference = 'Stop'
$CopyFlags = 1044 # FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI

function Get-ChildItemByName {
    param([object]$Folder, [string]$Name)
    @($Folder.Items()) | Where-Object { $_.Name -ieq $Name } | Select-Object -First 1
}

function Wait-LocalFile {
    param([string]$Path, [int]$Timeout)
    $until = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $until) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Wait-RemoteItem {
    param([object]$Folder, [string]$Name, [int]$Timeout, [switch]$Absent)
    $until = (Get-Date).AddSeconds($Timeout)
    do {
        $item = Get-ChildItemByName $Folder $Name
        if ($Absent -and $null -eq $item) { return $true }
        if (-not $Absent -and $null -ne $item) { return $item }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $until)
    if ($Absent) { return $false }
    return $null
}

function Copy-RemoteItemToLocal {
    param([object]$RemoteItem, [string]$LocalDirectory, [string]$ExpectedName, [int]$Timeout)
    $localFolder = $script:Shell.Namespace($LocalDirectory)
    if ($null -eq $localFolder) { throw "无法打开本地目录: $LocalDirectory" }
    $expectedPath = Join-Path $LocalDirectory $ExpectedName
    Remove-Item -LiteralPath $expectedPath -Force -ErrorAction SilentlyContinue
    $localFolder.CopyHere($RemoteItem, $script:CopyFlags)
    if (-not (Wait-LocalFile $expectedPath $Timeout)) {
        if ($RemoteItem.Path) { $localFolder.CopyHere($RemoteItem.Path, $script:CopyFlags) }
    }
    if (-not (Wait-LocalFile $expectedPath $Timeout)) {
        throw "从 MTP 读取文件超时: $ExpectedName"
    }
    return $expectedPath
}

function Remove-RemoteItem {
    param([object]$RemoteItem, [object]$Folder, [int]$Timeout)
    $verb = @($RemoteItem.Verbs()) | Where-Object {
        $_.Name.Trim() -match '^(delete|删除)$'
    } | Select-Object -First 1
    if ($null -eq $verb) { throw "找不到 MTP 删除操作: $($RemoteItem.Name)" }
    $verb.DoIt()
    if (-not (Wait-RemoteItem $Folder $RemoteItem.Name $Timeout -Absent)) {
        throw "删除 MTP 文件超时: $($RemoteItem.Name)"
    }
}

function Test-KmzFile {
    param([string]$Path)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = $null
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        $names = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/').ToLowerInvariant() })
        foreach ($name in $names) {
            if ($name.StartsWith('/') -or $name.Contains('../') -or $name.Contains('/..')) {
                throw "KMZ 包含不安全的路径：$name"
            }
        }
        if ($names -notcontains 'wpmz/template.kml') { throw 'KMZ 缺少 wpmz/template.kml' }
        if ($names -notcontains 'wpmz/waylines.wpml') { throw 'KMZ 缺少 wpmz/waylines.wpml' }
    } catch {
        throw "KMZ 校验失败: $($_.Exception.Message)"
    } finally {
        if ($null -ne $archive) { $archive.Dispose() }
    }
}

$SourceKmz = (Resolve-Path -LiteralPath $SourceKmz).Path
$RemoteFileName = if ($RemoteFileName) { $RemoteFileName } else { [IO.Path]::GetFileName($SourceKmz) }
if ([IO.Path]::GetExtension($RemoteFileName).ToLowerInvariant() -ne '.kmz') { throw 'RemoteFileName 必须使用 .kmz 扩展名' }
Test-KmzFile $SourceKmz
$sourceHash = (Get-FileHash -LiteralPath $SourceKmz -Algorithm SHA256).Hash

$script:Shell = New-Object -ComObject Shell.Application
$pc = $script:Shell.Namespace(17)
if ($null -eq $pc) { throw '无法打开 Windows 此电脑命名空间' }
$device = @($pc.Items()) | Where-Object { $_.Name -ieq $DeviceName } | Select-Object -First 1
if ($null -eq $device) { throw "未找到设备 '$DeviceName'。请先解锁 RC 2 并确认资源管理器可见。" }
$deviceFolder = $device.GetFolder
if ($null -eq $deviceFolder) { throw '设备没有可浏览的 WPD 文件夹' }

$storageItem = @($deviceFolder.Items()) | Where-Object {
    $_.Name -match '内部共享存储|Internal Shared Storage|Internal Storage|内部存储'
} | Select-Object -First 1
if ($null -eq $storageItem) {
    $candidates = @($deviceFolder.Items()) | Where-Object { $_.IsFolder }
    if ($candidates.Count -eq 1) { $storageItem = $candidates[0] }
}
if ($null -eq $storageItem) { throw '未找到 RC 2 的内部存储空间' }
$targetFolder = $storageItem.GetFolder
foreach ($segment in ($WaypointPath -split '[\\/]')) {
    if ([string]::IsNullOrWhiteSpace($segment)) { continue }
    $next = Get-ChildItemByName $targetFolder $segment
    if ($null -eq $next) { throw "MTP 目录不存在: $WaypointPath（缺少 $segment）" }
    $targetFolder = $next.GetFolder
    if ($null -eq $targetFolder) { throw "MTP 项不是目录: $segment" }
}

$existing = Get-ChildItemByName $targetFolder $RemoteFileName
$operationId = [Guid]::NewGuid().ToString('N')
$localWork = Join-Path ([IO.Path]::GetTempPath()) "dji-rc2-mtp-$operationId"
New-Item -ItemType Directory -Path $localWork -Force | Out-Null
$backupDir = Join-Path $BackupRoot (Get-Date -Format 'yyyyMMdd-HHmmss')

Write-Host "设备: $($device.Name)"
Write-Host "目标目录: $WaypointPath"
Write-Host "源文件 SHA-256: $sourceHash"
Write-Host "目标文件: $RemoteFileName"
if ($existing) { Write-Host "目标已存在: $($existing.Name)" }

if ($DryRun) {
    Write-Host 'DryRun：未写入、删除或备份任何文件。'
    Remove-Item -LiteralPath $localWork -Recurse -Force -ErrorAction SilentlyContinue
    return
}
if ($existing -and -not $ReplaceExisting) { throw '目标文件已存在。请显式指定 -ReplaceExisting；当前未修改设备。' }

try {
    if ($existing) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        $backupPath = Copy-RemoteItemToLocal $existing $backupDir $RemoteFileName $TimeoutSeconds
        $backupHash = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash
        Write-Host "已备份旧文件: $backupPath ($backupHash)"
    }

    $tempName = ".$RemoteFileName.uploading-$operationId.kmz"
    $tempSource = Join-Path $localWork $tempName
    Copy-Item -LiteralPath $SourceKmz -Destination $tempSource
    $targetFolder.CopyHere($tempSource, $CopyFlags)
    $remoteTemp = Wait-RemoteItem $targetFolder $tempName $TimeoutSeconds
    if ($null -eq $remoteTemp) { throw "上传临时文件超时: $tempName" }
    $readbackTempDir = Join-Path $localWork 'readback-temp'
    New-Item -ItemType Directory -Path $readbackTempDir -Force | Out-Null
    $readbackTemp = Copy-RemoteItemToLocal $remoteTemp $readbackTempDir $tempName $TimeoutSeconds
    $tempHash = (Get-FileHash -LiteralPath $readbackTemp -Algorithm SHA256).Hash
    if ($tempHash -ne $sourceHash) { throw "临时文件回读校验失败: $tempHash != $sourceHash" }
    Write-Host '临时文件上传及回读校验通过。'

    if ($existing) {
        if (-not $PSCmdlet.ShouldProcess($RemoteFileName, '删除旧 KMZ（已备份）')) { return }
        Remove-RemoteItem $existing $targetFolder $TimeoutSeconds
    }
    $targetFolder.CopyHere($SourceKmz, $CopyFlags)
    $remoteFinal = Wait-RemoteItem $targetFolder $RemoteFileName $TimeoutSeconds
    if ($null -eq $remoteFinal) { throw "上传最终文件超时: $RemoteFileName" }
    $readbackFinalDir = Join-Path $localWork 'readback-final'
    New-Item -ItemType Directory -Path $readbackFinalDir -Force | Out-Null
    $readbackFinal = Copy-RemoteItemToLocal $remoteFinal $readbackFinalDir $RemoteFileName $TimeoutSeconds
    $finalHash = (Get-FileHash -LiteralPath $readbackFinal -Algorithm SHA256).Hash
    if ($finalHash -ne $sourceHash) { throw "最终文件回读校验失败: $finalHash != $sourceHash" }
    Write-Host "上传完成，SHA-256 已验证: $finalHash"
    Remove-RemoteItem $remoteTemp $targetFolder $TimeoutSeconds
} finally {
    Remove-Item -LiteralPath $localWork -Recurse -Force -ErrorAction SilentlyContinue
}


