# hermes-slim installer for Windows x64.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Version vX.Y.Z] [-Dir $env:LOCALAPPDATA\hermes-slim] [-- <setup args>]
#
# Downloads one prebuilt bundle from github.com/talos-hq/hermes-slim releases,
# verifies its sha256, unpacks it, and runs hermes-slim-setup. Nothing is
# compiled and no package manager runs.
param(
    [string]$Version = "latest",
    [string]$Dir = (Join-Path $env:LOCALAPPDATA "hermes-slim"),
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$SetupArgs
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$repo = "talos-hq/hermes-slim"
$target = "win32-x64"

$api = if ($Version -eq "latest") { "https://api.github.com/repos/$repo/releases/latest" } else { "https://api.github.com/repos/$repo/releases/tags/$Version" }
$release = Invoke-RestMethod $api
$asset = $release.assets | Where-Object { $_.name -like "hermes-slim-*-$target.zip" } | Select-Object -First 1
if (-not $asset) { throw "no $target bundle in release $($release.tag_name)" }
$sum = $release.assets | Where-Object { $_.name -eq "$($asset.name).sha256" } | Select-Object -First 1

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("hermes-slim-" + [guid]::NewGuid())
New-Item -ItemType Directory $tmp | Out-Null
try {
    $zip = Join-Path $tmp $asset.name
    Write-Host "-> downloading $($asset.name)"
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    $expected = ((Invoke-WebRequest $sum.browser_download_url).Content -split '\s+')[0]
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { throw "sha256 mismatch" }

    Write-Host "-> installing to $Dir"
    New-Item -ItemType Directory -Force $Dir | Out-Null
    $new = Join-Path $Dir "new"; $current = Join-Path $Dir "current"; $old = Join-Path $Dir "old"
    Remove-Item -Recurse -Force $new, $old -ErrorAction SilentlyContinue
    Expand-Archive $zip -DestinationPath $new
    if (Test-Path $current) { Move-Item $current $old }
    Move-Item (Join-Path $new "hermes-slim") $current
    Remove-Item -Recurse -Force $new
    Get-ChildItem -Recurse $current | Unblock-File

    $bin = Join-Path $current "bin"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ";") -notcontains $bin) {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
    }
    $env:Path = "$env:Path;$bin"
    Write-Host "OK installed; 'hermes' is on your PATH (new terminals)"
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

& (Join-Path $bin "hermes-slim-setup.cmd") --service @SetupArgs
exit $LASTEXITCODE
