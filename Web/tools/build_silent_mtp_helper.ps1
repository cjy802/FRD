[CmdletBinding()]
param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$OutputPath = if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    Join-Path $PSScriptRoot 'bin\silent_mtp_replace.exe'
} else {
    $OutputPath
}
$clang = (Get-Command clang-cl.exe -ErrorAction Stop).Source
$sdkRoot = 'C:\Program Files (x86)\Windows Kits\10'
$sdkVersion = Get-ChildItem "$sdkRoot\Include" -Directory |
    Sort-Object Name -Descending |
    Select-Object -First 1 -ExpandProperty Name
$include = "$sdkRoot\Include\$sdkVersion"
$lib = "$sdkRoot\Lib\$sdkVersion\um\x64"
$source = Join-Path $PSScriptRoot 'silent_mtp_replace.cpp'
$output = [IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($output)) | Out-Null

& $clang /nologo /EHsc /std:c++17 `
    "/I$include\um" "/I$include\shared" `
    $source "/Fe:$output" /link "/LIBPATH:$lib" `
    ole32.lib shell32.lib
if ($LASTEXITCODE -ne 0) {
    throw "clang-cl 编译无提示 MTP 助手失败，退出码 $LASTEXITCODE。"
}
Write-Host "已生成：$output"

