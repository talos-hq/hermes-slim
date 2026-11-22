# Drives slim/install.ps1 against slim/tests/range_server.py: parallel ranges, dropped
# connections, a resumed killed download, a no-op rerun, a bad connection (an outage,
# stalled ranges, a checksum 503, a proxy that ignores ranges) and a corrupt download.
#
#   powershell -ExecutionPolicy Bypass -File slim/tests/install_ps1_test.ps1 [-Installer PATH] [-Python python]
#
# Exits non-zero on the first failed expectation. Needs Python 3 for the server.
param(
    [string]$Installer = (Join-Path $PSScriptRoot "..\install.ps1"),
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$hostExe = (Get-Process -Id $PID).Path
$asset = "hermes-slim-fixture-win32-x64.zip"
$work = Join-Path ([IO.Path]::GetTempPath()) ("hs-ps1-" + [guid]::NewGuid())
$release = Join-Path $work "release"
New-Item -ItemType Directory -Force (Join-Path $work "tree\hermes-slim\bin"), $release | Out-Null
Set-Content (Join-Path $work "tree\hermes-slim\bin\hermes.cmd") "@exit /b 0"
Set-Content (Join-Path $work "tree\hermes-slim\bin\hermes-slim-setup.cmd") "@exit /b 0"
$payload = New-Object byte[] (12MB); (New-Object Random 7).NextBytes($payload)
[IO.File]::WriteAllBytes((Join-Path $work "tree\hermes-slim\payload.bin"), $payload)
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = Join-Path $release $asset
[IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $work "tree"), $zip, [IO.Compression.CompressionLevel]::NoCompression, $false)
$digest = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
Set-Content "$zip.sha256" "$digest  $asset"
$size = (Get-Item $zip).Length

function Start-Server([string]$Extra = "") {
    Remove-Item (Join-Path $release "requests.log") -ErrorAction SilentlyContinue
    $out = Join-Path $work "port.txt"
    $argList = @((Join-Path $PSScriptRoot "range_server.py"), $release) + @($Extra -split ' ' | Where-Object { $_ })
    $proc = Start-Process $Python -ArgumentList $argList -RedirectStandardOutput $out -PassThru -NoNewWindow
    for ($i = 0; $i -lt 100 -and -not (Test-Path $out) -or -not (Get-Content $out -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 100 }
    $script:server = $proc
    "http://127.0.0.1:$((Get-Content $out | Select-Object -First 1).Trim())/$asset"
}
function Stop-Server { if ($script:server) { Stop-Process -Id $script:server.Id -Force -ErrorAction SilentlyContinue; $script:server = $null } }
function Get-Requests { @(Get-Content (Join-Path $release "requests.log") -ErrorAction SilentlyContinue | Where-Object { $_ -like "GET /$asset *" }) }
function Get-Starts { @(Get-Requests | Where-Object { $_ -match '=(\d+)-' -and $_ -notmatch '=0-0$' } | ForEach-Object { [long]([regex]::Match($_, '=(\d+)-').Groups[1].Value) }) }
function Install([string]$Dir, [string[]]$More) {
    $log = Join-Path $work "install.log"
    # Windows PowerShell 5.1 turns a child's stderr into a terminating error under Stop.
    $ErrorActionPreference = "Continue"
    & $hostExe -NoProfile -ExecutionPolicy Bypass -File $Installer -Dir $Dir -NoSetup @More *> $log
    $script:lastLog = Get-Content $log -Raw
    $LASTEXITCODE
}
function Expect([bool]$Ok, [string]$What) {
    if (-not $Ok) { Write-Host "FAIL: $What`n$script:lastLog"; Stop-Server; exit 1 }
    Write-Host "ok: $What"
}
function Expect-Installed([string]$Dir) {
    $tree = Join-Path $Dir "current"
    Expect ((Get-Content (Join-Path $tree ".bundle-sha256") -Raw).Trim() -eq $digest) "bundle sha256 recorded"
    Expect ((Get-Item (Join-Path $tree "payload.bin")).Length -eq 12MB) "payload intact"
    Expect (-not (Get-ChildItem (Join-Path $Dir "downloads"))) "download cache emptied"
}

try {
    $chunk = [long][Math]::Ceiling($size / 4)

    $url = Start-Server
    $dir = Join-Path $work "parallel"
    Expect ((Install $dir @("-Archive", $url, "-Jobs", "4")) -eq 0) "parallel install exits 0"
    Expect-Installed $dir
    Expect ((Get-Starts).Count -ge 4) "bundle fetched as 4+ ranges"
    $before = (Get-Requests).Count
    Expect ((Install $dir @("-Archive", $url, "-Jobs", "4")) -eq 0) "rerun exits 0"
    Expect ($script:lastLog -match "already installed") "rerun says already installed"
    Expect (@(Get-Requests | Select-Object -Skip $before | Where-Object { $_ -notmatch '=0-0$' }).Count -eq 0) "rerun downloads nothing"
    Stop-Server

    $url = Start-Server "--drop-after 1048576"
    $dir = Join-Path $work "dropped"
    Expect ((Install $dir @("-Archive", $url, "-Jobs", "4")) -eq 0) "install over dropped connections exits 0"
    Expect-Installed $dir
    $starts = Get-Starts
    Expect (@(0..3 | Where-Object { $starts -notcontains ($_ * $chunk + 1MB) }).Count -eq 0) "each range resumed at its 1 MiB cut"
    Stop-Server

    $url = Start-Server
    $dir = Join-Path $work "killed"
    $parts = Join-Path $dir "downloads\$asset.parts"
    New-Item -ItemType Directory -Force $parts | Out-Null
    Set-Content (Join-Path $parts "size") "$size"
    $bytes = [IO.File]::ReadAllBytes($zip)
    foreach ($i in 0..3) {
        $len = [Math]::Floor($chunk / 2)
        $piece = New-Object byte[] $len
        [Array]::Copy($bytes, $i * $chunk, $piece, 0, $len)
        [IO.File]::WriteAllBytes((Join-Path $parts "$i"), $piece)
    }
    Expect ((Install $dir @("-Archive", $url, "-Jobs", "4")) -eq 0) "resumed install exits 0"
    Expect-Installed $dir
    $want = @(0..3 | ForEach-Object { $_ * $chunk + [Math]::Floor($chunk / 2) })
    Expect ((@(Get-Starts | Sort-Object) -join ",") -eq ($want -join ",")) "killed download resumed mid-range"
    Stop-Server

    # A bad connection: each case is one way it breaks a download (range_server.py's faults).
    $url = Start-Server "--outage 10"
    $dir = Join-Path $work "outage"
    Expect ((Install $dir @("-Archive", $url, "-Jobs", "4")) -eq 0) "install through a 10 s outage exits 0"
    Expect-Installed $dir
    Stop-Server

    $url = Start-Server "--stall-after 1048576"
    $dir = Join-Path $work "stalled"
    Expect ((Install $dir @("-Archive", $url, "-Jobs", "4")) -eq 0) "install over stalled connections exits 0"
    Expect-Installed $dir
    $starts = Get-Starts
    Expect (@(0..3 | Where-Object { $starts -notcontains ($_ * $chunk + 1MB) }).Count -eq 0) "each stalled range resumed at its 1 MiB mark"
    Stop-Server

    $url = Start-Server "--flaky $asset.sha256"
    $dir = Join-Path $work "flaky-sha"
    Expect ((Install $dir @("-Archive", $url)) -eq 0) "install through a checksum 503 exits 0"
    # Expect-Installed checks .bundle-sha256: the checksum was retried, not skipped.
    Expect-Installed $dir
    Stop-Server

    $url = Start-Server "--ignore-ranges"
    $dir = Join-Path $work "no-ranges"
    Expect ((Install $dir @("-Archive", $url, "-Jobs", "4")) -eq 0) "install through a range-ignoring proxy exits 0"
    Expect-Installed $dir
    Stop-Server

    Set-Content "$zip.sha256" ("0" * 64 + "  $asset")
    $url = Start-Server
    $dir = Join-Path $work "corrupt"
    Expect ((Install $dir @("-Archive", $url)) -ne 0) "corrupt download fails"
    Expect ($script:lastLog -match "sha256 mismatch") "corrupt download says sha256 mismatch"
    Expect (-not (Test-Path (Join-Path $dir "current"))) "corrupt download installs nothing"
    Write-Host "all install.ps1 checks passed"
} finally {
    Stop-Server
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
