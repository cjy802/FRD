[CmdletBinding()]
param(
    [string]$SourceKmz,
    [string]$DeviceName = 'DJI RC 2',
    [string]$RemoteFileName,
    [string]$WaypointPath = 'Android/data/dji.go.v5/files/waypoint',
    [string]$RemoteSubFolder,
    [string]$BackupRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'DjiRc2MtpBackups'),
    [int]$TimeoutSeconds = 60,
    [switch]$ReplaceExisting,
    [switch]$ListOnly,
    [switch]$DryRun,
    [switch]$Json
)

# MTP devices do not have drive letters. Windows Shell/WPD lists their files;
# replace/backup/readback all use live Folder objects + CopyHere, because the
# device's Path strings cannot be re-bound (SHCreateItemFromParsingName
# returns E_INVALIDARG for them on some devices). CopyHere over an existing
# MTP file always shows a Windows replace-confirmation dialog (silent flags
# and the delete verb are equally modal on this device), so the UI warns the
# user to expect it and confirm.
$ErrorActionPreference = 'Stop'
if ($Json) { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) }
$script:CopyFlags = 1044 # FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
$script:Shell = $null
$localWork = $null
$backupPath = $null
$backupHash = $null
$replaceStarted = $false
$result = [ordered]@{
    status = 'error'
    stage = 'initializing'
    failure_stage = $null
    recovery_stage = $null
    message = $null
    device_name = $DeviceName
    waypoint_path = $WaypointPath
    remote_file_name = $null
    source_sha256 = $null
    readback_sha256 = $null
    backup_path = $null
    backup_sha256 = $null
    restore_attempted = $false
    restore_verified = $false
    remote_state = 'unchanged'
    targets = @()
    folders = @()
}

function Write-ProgressMessage {
    param([string]$Message)
    if (-not $Json) { Write-Host $Message }
}

function Test-RemoteFileName {
    param([string]$Name)
    return -not [string]::IsNullOrWhiteSpace($Name) -and
        $Name -match '^[^\\/:*?"<>|\x00-\x1f]+\.kmz$' -and
        $Name -notmatch '\.\.' -and
        $Name -eq [IO.Path]::GetFileName($Name)
}

# DJI Fly 把每条占位航线存成 waypoint/<UUID>/ 文件夹；替换必须写入与
# 文件夹同名的 <UUID>.kmz 才会被识别。
$script:UuidFolderPattern = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

function Find-RemoteKmzFiles {
    # 在占位航线文件夹里递归找 .kmz（最多 3 层），返回 name + 相对子路径。
    param([object]$Folder, [string]$Prefix = '', [int]$Depth = 0)
    $found = @()
    if ($Depth -gt 3) { return $found }
    foreach ($item in @($Folder.Items())) {
        $relative = if ($Prefix) { "$Prefix/$($item.Name)" } else { $item.Name }
        if ($item.IsFolder) {
            $found += Find-RemoteKmzFiles -Folder $item.GetFolder() -Prefix $relative -Depth ($Depth + 1)
        } elseif (Test-RemoteFileName $item.Name) {
            $found += [PSCustomObject]@{ name = $item.Name; subpath = $Prefix; type = $item.Type }
        }
    }
    return $found
}

function Get-ChildItemByName {
    param([object]$Folder, [string]$Name)
    @($Folder.Items()) | Where-Object { $_.Name -ieq $Name } | Select-Object -First 1
}

function Wait-RemoteItem {
    param([object]$Folder, [string]$Name, [int]$Timeout)
    $until = (Get-Date).AddSeconds($Timeout)
    do {
        $item = Get-ChildItemByName $Folder $Name
        if ($null -ne $item) { return $item }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $until)
    return $null
}

function Test-KmzFile {
    param([string]$Path, [switch]$RequireMissionFiles)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = $null
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        if ($archive.Entries.Count -eq 0) { throw 'ZIP 内容为空' }
        $names = @()
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace('\', '/').ToLowerInvariant()
            if ($name.StartsWith('/') -or $name.Contains('../') -or $name.EndsWith('/..') -or $name.Contains(':')) {
                throw "KMZ 包含不安全的路径：$name"
            }
            $names += $name
            if (-not $name.EndsWith('/')) {
                # Shell.CopyHere is asynchronous. Reading every entry catches an
                # incomplete local copy before we trust it as a backup/readback.
                $entryStream = $entry.Open()
                try { $entryStream.CopyTo([IO.Stream]::Null) }
                finally { $entryStream.Dispose() }
            }
        }
        if ($RequireMissionFiles) {
            if ($names -notcontains 'wpmz/template.kml') { throw 'KMZ 缺少 wpmz/template.kml' }
            if ($names -notcontains 'wpmz/waylines.wpml') { throw 'KMZ 缺少 wpmz/waylines.wpml' }
        }
    } catch {
        throw "KMZ 校验失败: $($_.Exception.Message)"
    } finally {
        if ($null -ne $archive) { $archive.Dispose() }
    }
}

function Wait-CompleteLocalKmz {
    param([string]$Path, [int]$Timeout)
    $until = (Get-Date).AddSeconds($Timeout)
    $lastSize = -1
    do {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            try {
                $size = (Get-Item -LiteralPath $Path).Length
                if ($size -gt 0) {
                    Test-KmzFile $Path
                    if ($size -eq $lastSize) { return $true }
                    $lastSize = $size
                }
            } catch {
                # A file may exist while MTP is still copying it.
            }
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $until)
    return $false
}

function Copy-RemoteItemToLocal {
    param([object]$RemoteItem, [string]$LocalDirectory, [string]$ExpectedName, [int]$Timeout)
    $localFolder = $script:Shell.Namespace($LocalDirectory)
    if ($null -eq $localFolder) { throw "无法打开本地目录: $LocalDirectory" }
    $expectedPath = Join-Path $LocalDirectory $ExpectedName
    Remove-Item -LiteralPath $expectedPath -Force -ErrorAction SilentlyContinue
    $localFolder.CopyHere($RemoteItem, $script:CopyFlags)
    if (-not (Wait-CompleteLocalKmz $expectedPath $Timeout)) {
        if (Test-Path -LiteralPath $expectedPath -PathType Leaf) {
            throw "从 MTP 复制的 KMZ 未完整写入: $ExpectedName"
        }
        if ($RemoteItem.Path) {
            $localFolder.CopyHere($RemoteItem.Path, $script:CopyFlags)
        }
        if (-not (Wait-CompleteLocalKmz $expectedPath $Timeout)) {
            throw "从 MTP 读取文件超时: $ExpectedName"
        }
    }
    return $expectedPath
}

function Get-RemoteFileHash {
    param([object]$Folder, [string]$Name, [string]$LocalDirectory, [int]$Timeout)
    $remoteItem = Wait-RemoteItem $Folder $Name $Timeout
    if ($null -eq $remoteItem -or $remoteItem.IsFolder) { throw "MTP 文件不存在: $Name" }
    New-Item -ItemType Directory -Path $LocalDirectory -Force | Out-Null
    $localPath = Copy-RemoteItemToLocal $remoteItem $LocalDirectory $Name $Timeout
    return (Get-FileHash -LiteralPath $localPath -Algorithm SHA256).Hash
}

function Copy-LocalToRemoteFolder {
    # CopyHere 按源文件名写入；先把本地文件暂存成远端目标名，再复制。
    # CopyHere 是异步的；复制后轮询回读，直到远端内容与本地一致或超时。
    # 回读放在单独的子目录：Get-RemoteFileHash 会先删除目标路径上的文件，
    # 不能和暂存源文件共用同一目录。
    param([object]$Folder, [string]$LocalPath, [string]$Name, [string]$ExpectedHash,
          [string]$WorkDirectory, [int]$Timeout)
    New-Item -ItemType Directory -Path $WorkDirectory -Force | Out-Null
    $staged = Join-Path $WorkDirectory $Name
    if ((Split-Path $LocalPath -Leaf) -ine $Name) {
        Copy-Item -LiteralPath $LocalPath -Destination $staged -Force
        $LocalPath = $staged
    }
    $readbackDir = Join-Path $WorkDirectory 'readback'
    $Folder.CopyHere($LocalPath, $script:CopyFlags)
    $deadline = (Get-Date).AddSeconds($Timeout)
    $hash = $null
    do {
        Start-Sleep -Milliseconds 900
        $hash = $null
        try { $hash = Get-RemoteFileHash $Folder $Name $readbackDir $Timeout } catch { }
    } while ($hash -ne $ExpectedHash -and (Get-Date) -lt $deadline)
    return $hash
}

try {
    if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 600) {
        throw 'TimeoutSeconds 必须在 1 到 600 秒之间'
    }
    foreach ($segment in ($WaypointPath -split '[\\/]')) {
        if ($segment -eq '.' -or $segment -eq '..') { throw 'WaypointPath 不能包含 . 或 .. 路径段' }
    }
    foreach ($segment in ($RemoteSubFolder -split '[\\/]')) {
        if ([string]::IsNullOrWhiteSpace($segment)) { continue }
        if ($segment -eq '.' -or $segment -eq '..') { throw 'RemoteSubFolder 不能包含 . 或 .. 路径段' }
    }

    if (-not $ListOnly) {
        $result.stage = 'validating_source'
        if ([string]::IsNullOrWhiteSpace($SourceKmz) -or -not (Test-Path -LiteralPath $SourceKmz -PathType Leaf)) {
            throw 'SourceKmz 必须是已存在的本地 KMZ 文件'
        }
        $SourceKmz = (Resolve-Path -LiteralPath $SourceKmz).Path
        $RemoteFileName = if ($RemoteFileName) { $RemoteFileName } else { [IO.Path]::GetFileName($SourceKmz) }
        if (-not (Test-RemoteFileName $RemoteFileName)) {
            throw 'RemoteFileName 必须是不含路径的 .kmz 文件名'
        }
        $result.remote_file_name = $RemoteFileName
        Test-KmzFile $SourceKmz -RequireMissionFiles
        $result.source_sha256 = (Get-FileHash -LiteralPath $SourceKmz -Algorithm SHA256).Hash
    }

    $result.stage = 'discovering_device'
    $script:Shell = New-Object -ComObject Shell.Application
    $pc = $script:Shell.Namespace(17)
    if ($null -eq $pc) { throw '无法打开 Windows 此电脑命名空间' }
    $device = @($pc.Items()) | Where-Object { $_.Name -ieq $DeviceName } | Select-Object -First 1
    if ($null -eq $device) { throw "未找到设备 '$DeviceName'。请先解锁 RC 2 并确认资源管理器可见。" }
    $deviceFolder = $device.GetFolder
    if ($null -eq $deviceFolder) { throw '设备没有可浏览的 WPD 文件夹' }
    $result.device_name = $device.Name

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
    if (-not [string]::IsNullOrWhiteSpace($RemoteSubFolder)) {
        foreach ($segment in ($RemoteSubFolder -split '[\\/]')) {
            if ([string]::IsNullOrWhiteSpace($segment)) { continue }
            $next = Get-ChildItemByName $targetFolder $segment
            if ($null -eq $next) { throw "MTP 目录不存在: $WaypointPath/$RemoteSubFolder（缺少 $segment）" }
            $targetFolder = $next.GetFolder()
            if ($null -eq $targetFolder) { throw "MTP 项不是目录: $segment" }
        }
    }

    if ($ListOnly) {
        $result.stage = 'list'
        $result.targets = @(
            @($targetFolder.Items()) | Where-Object {
                -not $_.IsFolder -and (Test-RemoteFileName $_.Name)
            } | ForEach-Object {
                [PSCustomObject]@{ name = $_.Name; type = $_.Type }
            }
        )
        $result.folders = @(
            @($targetFolder.Items()) | Where-Object {
                $_.IsFolder -and $_.Name -match $script:UuidFolderPattern
            } | ForEach-Object {
                $folderName = $_.Name
                foreach ($kmz in (Find-RemoteKmzFiles -Folder $_.GetFolder())) {
                    [PSCustomObject]@{
                        folder = $folderName
                        name = $kmz.name
                        subpath = $kmz.subpath
                        name_matches_folder = ($kmz.name -ieq "$folderName.kmz")
                    }
                }
            }
        )
        $result.status = 'success'
        if (-not $Json) {
            Write-Host "设备: $($result.device_name)"
            Write-Host "目标目录: $WaypointPath"
            $result.targets | Format-Table -AutoSize
            $result.folders | Format-Table -AutoSize
        }
    } else {
        $result.stage = 'checking_target'
        $existing = Get-ChildItemByName $targetFolder $RemoteFileName
        if ($null -eq $existing -or $existing.IsFolder) {
            throw "目标航线文件不存在: $RemoteFileName。请先在 DJI Fly 中建立占位航线。"
        }
        if (-not $ReplaceExisting -and -not $DryRun) {
            throw '需要显式指定 -ReplaceExisting；当前未修改设备。'
        }
        Write-ProgressMessage "设备: $($result.device_name)"
        Write-ProgressMessage "目标目录: $WaypointPath"
        Write-ProgressMessage "源文件 SHA-256: $($result.source_sha256)"
        Write-ProgressMessage "现有目标文件: $RemoteFileName"

        if ($DryRun) {
            $result.stage = 'dry_run'
            $result.status = 'success'
            Write-ProgressMessage 'DryRun：目标存在；未写入、删除或备份文件。'
        } else {
            $operationId = [Guid]::NewGuid().ToString('N')
            $localWork = Join-Path ([IO.Path]::GetTempPath()) "dji-rc2-mtp-$operationId"
            New-Item -ItemType Directory -Path $localWork -Force | Out-Null
            $backupDir = Join-Path $BackupRoot "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$operationId"

            $result.stage = 'backing_up'
            New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
            $backupPath = Copy-RemoteItemToLocal $existing $backupDir $RemoteFileName $TimeoutSeconds
            $backupHash = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash
            $result.stage = 'verifying_backup'
            $backupCheckDir = Join-Path $localWork 'backup-check'
            $backupCheckHash = Get-RemoteFileHash $targetFolder $RemoteFileName $backupCheckDir $TimeoutSeconds
            if ($backupCheckHash -ne $backupHash) {
                throw "两次读取旧文件的哈希不一致: $backupHash != $backupCheckHash"
            }
            $result.backup_path = $backupPath
            $result.backup_sha256 = $backupHash
            Write-ProgressMessage "旧文件备份并校验完成: $backupPath ($backupHash)"

            $result.stage = 'replacing'
            $result.remote_state = 'unknown'
            $replaceStarted = $true
            Write-ProgressMessage "通过 MTP 复制新航线文件，替换 ${RemoteFileName}。"
            $replaceDir = Join-Path $localWork 'replace-readback'
            $replaceHash = Copy-LocalToRemoteFolder -Folder $targetFolder -LocalPath $SourceKmz `
                -Name $RemoteFileName -ExpectedHash $result.source_sha256 `
                -WorkDirectory $replaceDir -Timeout $TimeoutSeconds
            if ($replaceHash -ne $result.source_sha256) {
                throw "替换后的回读哈希不一致: $replaceHash != $($result.source_sha256)"
            }

            $result.stage = 'verifying'
            $readbackDir = Join-Path $localWork 'readback-final'
            $finalHash = Get-RemoteFileHash $targetFolder $RemoteFileName $readbackDir $TimeoutSeconds
            $result.readback_sha256 = $finalHash
            if ($finalHash -ne $result.source_sha256) {
                throw "最终文件回读校验失败: $finalHash != $($result.source_sha256)"
            }
            $result.status = 'success'
            $result.stage = 'verified'
            $result.remote_state = 'verified'
            Write-ProgressMessage "上传完成，SHA-256 已验证: $finalHash"
        }
    }
} catch {
    $originalMessage = $_.Exception.Message
    $result.failure_stage = $result.stage
    if ($replaceStarted -and $backupPath -and $backupHash) {
        try {
            $probeDir = Join-Path $localWork 'readback-before-restore'
            $observedHash = $null
            try { $observedHash = Get-RemoteFileHash $targetFolder $RemoteFileName $probeDir $TimeoutSeconds }
            catch { }
            if ($observedHash -eq $backupHash) {
                $result.remote_state = 'original'
                $result.restore_verified = $true
                $result.recovery_stage = 'original_verified'
            } else {
                $result.recovery_stage = 'restoring'
                $result.restore_attempted = $true
                $restoreDir = Join-Path $localWork 'readback-restored'
                $restoredHash = Copy-LocalToRemoteFolder -Folder $targetFolder -LocalPath $backupPath `
                    -Name $RemoteFileName -ExpectedHash $backupHash `
                    -WorkDirectory $restoreDir -Timeout $TimeoutSeconds
                if ($restoredHash -ne $backupHash) { throw "恢复后哈希不匹配: $restoredHash != $backupHash" }
                $result.remote_state = 'restored'
                $result.restore_verified = $true
                $result.recovery_stage = 'restored_verified'
            }
        } catch {
            $result.remote_state = 'unknown'
            $result.restore_verified = $false
            $result.recovery_stage = 'failed'
            $originalMessage = "$originalMessage；恢复或验证旧文件失败: $($_.Exception.Message)"
        }
    }
    $result.status = 'error'
    $result.message = $originalMessage
} finally {
    if ($localWork -and (Test-Path -LiteralPath $localWork -PathType Container)) {
        Remove-Item -LiteralPath $localWork -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue
    }
}

if ($Json) {
    $result | ConvertTo-Json -Depth 6 -Compress | Write-Output
} elseif ($result.status -eq 'error') {
    [Console]::Error.WriteLine("$($result.stage): $($result.message)")
}
if ($result.status -eq 'error') { exit 1 }
exit 0
