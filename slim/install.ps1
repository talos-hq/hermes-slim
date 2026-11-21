# hermes-slim installer for Windows x64.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Version vX.Y.Z | -Archive FILE|URL]
#       [-Dir $env:LOCALAPPDATA\hermes-slim] [-Jobs N] [-Upgrade] [-NoSetup] [-- <setup args>]
#
# Downloads one prebuilt bundle from github.com/talos-hq/hermes-slim releases,
# verifies its sha256, unpacks it, and runs hermes-slim-setup. Nothing is
# compiled and no package manager runs.
#
# The bundle downloads over -Jobs parallel byte ranges (default 8). Every stage resumes:
# the download lives in <Dir>\downloads until it is installed, each range continues from
# where it stopped, and a rerun skips a download or unpack that already finished for the
# same sha256. The bundle is a .tar.zst (a .zip for releases before #141), unpacked with
# the release's standalone zstd-win32-x64.exe and Windows' own tar.exe. -Archive installs
# a bundle from disk (with zstd-win32-x64.exe beside it) or any HTTP(S) mirror (URL.sha256
# is checked when present); -Upgrade keeps the configuration and only restarts the
# gateway; -NoSetup stops after the files and PATH are in place.
param(
    [string]$Version = "latest",
    [string]$Dir = (Join-Path $env:LOCALAPPDATA "hermes-slim"),
    [string]$Archive = "",
    [ValidateRange(1, 32)][int]$Jobs = 8,
    [switch]$Upgrade,
    [switch]$NoSetup,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$SetupArgs
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$repo = "talos-hq/hermes-slim"
$target = "win32-x64"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
# .NET Framework allows 2 connections per host by default, which would serialize the ranges.
[Net.ServicePointManager]::DefaultConnectionLimit = [Math]::Max([Net.ServicePointManager]::DefaultConnectionLimit, $Jobs + 2)

function Get-Sha256([string]$Path) { (Get-FileHash $Path -Algorithm SHA256).Hash.ToLower() }

function Get-Text([string]$Url) {
    $client = New-Object Net.WebClient
    try { $client.DownloadString($Url) } finally { $client.Dispose() }
}

# Total size when the server honours byte ranges (GitHub's release CDN does), else 0.
function Get-RangeSize([string]$Url) {
    try {
        $req = [Net.HttpWebRequest]::Create($Url)
        $req.AddRange([long]0, [long]0)
        $resp = $req.GetResponse()
        try {
            if ([int]$resp.StatusCode -eq 206 -and $resp.Headers["Content-Range"] -match '/(\d+)$') { return [long]$Matches[1] }
        } finally { $resp.Dispose() }
    } catch { }
    return [long]0
}

# Appends bytes From+<already on disk>..To of Url to Path. Runs inside a runspace.
$FetchRange = {
    param([string]$Url, [string]$Path, [long]$From, [long]$To)
    $have = if (Test-Path -LiteralPath $Path) { (Get-Item -LiteralPath $Path).Length } else { [long]0 }
    if ($have -gt ($To - $From + 1)) { Remove-Item -LiteralPath $Path; $have = [long]0 }
    if ($have -eq ($To - $From + 1)) { return }
    $req = [Net.HttpWebRequest]::Create($Url)
    $req.AddRange([long]($From + $have), [long]$To)
    $req.Timeout = 30000
    $req.ReadWriteTimeout = 60000
    $resp = $req.GetResponse()
    try {
        if ([int]$resp.StatusCode -ne 206) { throw "server ignored the byte range" }
        $in = $resp.GetResponseStream()
        $out = [IO.File]::Open($Path, [IO.FileMode]::Append, [IO.FileAccess]::Write)
        try { $in.CopyTo($out, 1MB) } finally { $out.Dispose(); $in.Dispose() }
    } finally { $resp.Dispose() }
}

function Receive-Ranges([string]$Url, [string]$Out, [long]$Size) {
    $parts = "$Out.parts"
    $sizeFile = Join-Path $parts "size"
    if (-not (Test-Path $sizeFile) -or (Get-Content $sizeFile) -ne "$Size") {
        Remove-Item -Recurse -Force $parts -ErrorAction SilentlyContinue
        New-Item -ItemType Directory $parts | Out-Null
        Set-Content $sizeFile "$Size"
    }
    $chunk = [long][Math]::Ceiling($Size / $Jobs)
    $ranges = for ($from = [long]0; $from -lt $Size; $from += $chunk) {
        @{ Path = Join-Path $parts ([string]($from / $chunk)); From = $from; To = [Math]::Min($from + $chunk, $Size) - 1 }
    }
    $pool = [RunspaceFactory]::CreateRunspacePool(1, $Jobs)
    $pool.Open()
    try {
        for ($round = 0; ; $round++) {
            $todo = @($ranges | Where-Object { -not (Test-Path $_.Path) -or (Get-Item $_.Path).Length -ne ($_.To - $_.From + 1) })
            if (-not $todo) { break }
            if ($round -ge 5) { throw "download of $Url keeps failing; rerun to resume it" }
            $running = foreach ($r in $todo) {
                $ps = [PowerShell]::Create().AddScript($FetchRange).AddArgument($Url).AddArgument($r.Path).AddArgument($r.From).AddArgument($r.To)
                $ps.RunspacePool = $pool
                @{ PS = $ps; Handle = $ps.BeginInvoke() }
            }
            while (@($running | Where-Object { -not $_.Handle.IsCompleted }).Count) {
                $got = ($ranges | ForEach-Object { if (Test-Path $_.Path) { (Get-Item $_.Path).Length } else { 0 } } | Measure-Object -Sum).Sum
                Write-Host -NoNewline ("`r  {0,3}%  {1} / {2} MiB" -f [int]($got * 100 / $Size), [int]($got / 1MB), [int]($Size / 1MB))
                Start-Sleep -Milliseconds 500
            }
            foreach ($j in $running) {
                try { $j.PS.EndInvoke($j.Handle) | Out-Null } catch { Write-Host "`n  retrying a range: $($_.Exception.InnerException.Message)" }
                $j.PS.Dispose()
            }
        }
    } finally { $pool.Dispose() }
    Write-Host ""
    $dest = [IO.File]::Create("$Out.tmp")
    try {
        foreach ($r in $ranges) {
            $src = [IO.File]::OpenRead($r.Path)
            try { $src.CopyTo($dest, 1MB) } finally { $src.Dispose() }
        }
    } finally { $dest.Dispose() }
    Move-Item -Force "$Out.tmp" $Out
    Remove-Item -Recurse -Force $parts
}

# Skips Out when it already has Sha256; otherwise downloads it (in parallel ranges when it is
# large and the server allows it) and checks Sha256.
function Receive-File([string]$Url, [string]$Out, [string]$Sha256) {
    $name = Split-Path -Leaf $Out
    if ($Sha256 -and (Test-Path $Out) -and (Get-Sha256 $Out) -eq $Sha256) { Write-Host "-> $name already downloaded"; return }
    Remove-Item -Force $Out -ErrorAction SilentlyContinue
    $size = if ($Jobs -gt 1) { Get-RangeSize $Url } else { [long]0 }
    if ($size -ge 8MB) {
        Write-Host "-> downloading $name ($([int]($size / 1MB)) MiB, $Jobs connections)"
        Receive-Ranges $Url $Out $size
    } else {
        Write-Host "-> downloading $name"
        Invoke-WebRequest $Url -OutFile "$Out.part" -UseBasicParsing
        Move-Item -Force "$Out.part" $Out
    }
    if ($Sha256 -and (Get-Sha256 $Out) -ne $Sha256) {
        Remove-Item -Force $Out
        throw "sha256 mismatch for $name; rerun to download it again"
    }
}

$downloads = Join-Path $Dir "downloads"
New-Item -ItemType Directory -Force $downloads | Out-Null
$current = Join-Path $Dir "current"; $new = Join-Path $Dir "hermes-slim.new"; $old = Join-Path $Dir "hermes-slim.old"
$expected = ""
# The sha256 recorded in an unpacked tree marks it complete; an interrupted unpack has none.
function Test-Installed([string]$Tree) {
    $marker = Join-Path $Tree ".bundle-sha256"
    $expected -and (Test-Path $marker) -and (Get-Content $marker -Raw).Trim() -eq $expected
}
function Test-NeedBundle { -not (Test-Installed $current) -and -not (Test-Installed (Join-Path $new "hermes-slim")) }

# A .tar.zst bundle unpacks with a standalone zstd.exe piped into Windows' own tar.exe
# (bsdtar, in System32 since Windows 10 1803). The helper downloads in the background
# while the bundle's ranges do (#141, #144) and is checked against its sha256 before use.
$zstdAsset = "zstd-$target.exe"
$script:helperJob = $null
function Start-Helper([string]$Base, [string]$Sha256) {
    if (Get-Command zstd.exe -ErrorAction SilentlyContinue) { return }
    $dest = Join-Path $downloads $zstdAsset
    Remove-Item -Force $dest -ErrorAction SilentlyContinue
    $client = New-Object Net.WebClient
    $script:helperJob = @{ Client = $client; Task = $client.DownloadFileTaskAsync("$Base/$zstdAsset", $dest)
                           Path = $dest; Base = $Base; Sha256 = $Sha256 }
}
function Get-Helper([string]$ArchivePath) {
    $onPath = Get-Command zstd.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $job = $script:helperJob
    if ($job) {
        try { $job.Task.Wait() }
        catch { throw "could not download $zstdAsset from $($job.Base): $($_.Exception.GetBaseException().Message)" }
        finally { $job.Client.Dispose() }
        $want = if ($job.Sha256) { $job.Sha256 } else { ((Get-Text "$($job.Base)/$zstdAsset.sha256") -split '\s+')[0] }
        $path = $job.Path
    } else {
        $path = Join-Path (Split-Path $ArchivePath) $zstdAsset
        if (-not (Test-Path "$path.sha256")) { throw "no zstd here: put $zstdAsset and $zstdAsset.sha256 beside the archive" }
        $want = ((Get-Content "$path.sha256" -Raw) -split '\s+')[0]
    }
    if ((Get-Sha256 $path) -ne $want) { throw "$zstdAsset sha256 mismatch" }
    $path
}
function Expand-Bundle([string]$ArchivePath, [string]$Dest) {
    if ($ArchivePath.EndsWith(".zip")) {
        # Releases before the .tar.zst switch (#141) ship only a zip.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($ArchivePath, $Dest)
        return
    }
    New-Item -ItemType Directory -Force $Dest | Out-Null
    $zstd = Get-Helper $ArchivePath
    $tar = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path $tar)) { throw "unpacking needs $tar (Windows 10 1803 or newer)" }
    # cmd pipes raw bytes; a PowerShell pipeline between two native programs re-encodes them
    # as text. Start-Process hands cmd this command line verbatim (/s: strip only the outer quotes).
    $line = "/s /c `"`"$zstd`" -dcq `"$ArchivePath`" | `"$tar`" -xf - -C `"$Dest`"`""
    $proc = Start-Process cmd.exe -ArgumentList $line -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode) { throw "unpacking $ArchivePath failed (exit $($proc.ExitCode))" }
}

if ($Archive -match '^https?://') {
    if ($Archive.EndsWith(".tar.zst")) { Start-Helper ($Archive -replace '/[^/]*$', '') "" }
    try { $expected = ((Get-Text "$Archive.sha256") -split '\s+')[0] } catch { }
    $local = Join-Path $downloads ($Archive -split '/')[-1]
    if (-not $expected -or (Test-NeedBundle)) { Receive-File $Archive $local $expected }
    $Archive = $local
} elseif ($Archive) {
    if (-not (Test-Path $Archive)) { throw "no such archive: $Archive" }
    if (Test-Path "$Archive.sha256") {
        $expected = ((Get-Content "$Archive.sha256" -Raw) -split '\s+')[0]
        if ((Get-Sha256 $Archive) -ne $expected) { throw "sha256 mismatch" }
    }
} else {
    # The release manifest (slim/release_manifest.py) is served by the release CDN, which,
    # unlike api.github.com (60 unauthenticated calls per hour per IP), does not run out on
    # a shared network (#146). It names the tag, so every download pins to one release.
    $manifestBase = if ($Version -eq "latest") { "https://github.com/$repo/releases/latest/download" } else { "https://github.com/$repo/releases/download/$Version" }
    $manifest = $null
    try { $manifest = Get-Text "$manifestBase/hermes-slim-manifest.txt" } catch { }
    if ($manifest) {
        $lines = $manifest -split "`r?`n"
        $tagLine = $lines | Where-Object { $_ -match '^release (\S+)$' } | Select-Object -First 1
        if ($tagLine -match '^release (\S+)$') { $Version = $Matches[1] }
        $entry = @($lines | Where-Object { $_ -match "^bundle $target hermes-slim-\S+\.tar\.zst [0-9a-f]{64} " }) +
                 @($lines | Where-Object { $_ -match "^bundle $target hermes-slim-\S+\.zip [0-9a-f]{64} " }) | Select-Object -First 1
        if (-not $entry) { throw "no $target bundle in release $Version" }
        $null = $entry -match "^bundle $target (\S+) (\S+) "
        $name = $Matches[1]; $expected = $Matches[2]
        $helperLine = $lines | Where-Object { $_ -match "^zstd $target $([regex]::Escape($zstdAsset)) ([0-9a-f]{64}) " } | Select-Object -First 1
        $helperSha = if ($helperLine -match " ([0-9a-f]{64}) ") { $Matches[1] } else { "" }
    } else {
        # Releases from before the manifest: look the asset up through the API.
        $api = if ($Version -eq "latest") { "https://api.github.com/repos/$repo/releases/latest" } else { "https://api.github.com/repos/$repo/releases/tags/$Version" }
        try { $release = Invoke-RestMethod $api }
        catch {
            $status = [int]$_.Exception.Response.StatusCode
            if ($status -eq 403 -or $status -eq 429) { throw "GitHub's API rate limit for this network is used up; retry later, or pass -Archive URL" }
            throw
        }
        $Version = $release.tag_name
        $asset = @($release.assets | Where-Object { $_.name -like "hermes-slim-*-$target.tar.zst" }) +
                 @($release.assets | Where-Object { $_.name -like "hermes-slim-*-$target.zip" }) | Select-Object -First 1
        $helperSha = ""
        if (-not $asset) { throw "no $target bundle in release $Version" }
        $name = $asset.name
        $expected = ((Get-Text "https://github.com/$repo/releases/download/$Version/$name.sha256") -split '\s+')[0]
    }
    $Archive = Join-Path $downloads $name
    if ($name.EndsWith(".tar.zst") -and (Test-NeedBundle)) { Start-Helper "https://github.com/$repo/releases/download/$Version" $helperSha }
    if (Test-NeedBundle) { Receive-File "https://github.com/$repo/releases/download/$Version/$name" $Archive $expected }
}

if (Test-Installed $current) {
    Write-Host "-> this bundle is already installed in $current"
} else {
    if (Test-Installed (Join-Path $new "hermes-slim")) {
        Write-Host "-> reusing the bundle unpacked by an earlier run"
    } else {
        Write-Host "-> installing to $Dir"
        Remove-Item -Recurse -Force $new -ErrorAction SilentlyContinue
        # A browser-downloaded -Archive carries the internet zone mark; clear it so extraction
        # is not slowed or blocked by it. Neither unpacker copies the mark onto the tree.
        Unblock-File $Archive
        Expand-Bundle $Archive $new
        if ($expected) { Set-Content (Join-Path $new "hermes-slim\.bundle-sha256") $expected }
    }
    Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue
    if (Test-Path $current) {
        # A running gateway holds files in the old tree open, and Windows cannot move them.
        try { & (Join-Path $current "bin\hermes.cmd") gateway stop *> $null } catch { }
        Move-Item $current $old
    }
    Move-Item (Join-Path $new "hermes-slim") $current
    Remove-Item -Recurse -Force $new
}
# The cached download (and the helper that unpacked it) has served its purpose.
if ($Archive.StartsWith($downloads)) { Remove-Item -Force $Archive -ErrorAction SilentlyContinue }
Remove-Item -Force (Join-Path $downloads $zstdAsset) -ErrorAction SilentlyContinue

$bin = Join-Path $current "bin"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $bin) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
}
$env:Path = "$env:Path;$bin"
Write-Host "OK installed; 'hermes' is on your PATH (new terminals)"

if ($NoSetup) { exit 0 }
$setupArgs = @("--service") + @($SetupArgs | Where-Object { $_ })
if ($Upgrade) { $setupArgs = @("--gateway-only") + $setupArgs }
& (Join-Path $bin "hermes-slim-setup.cmd") @setupArgs
exit $LASTEXITCODE
