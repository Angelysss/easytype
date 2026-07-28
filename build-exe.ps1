$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCommand) {
    $uvPath = $uvCommand.Source
} else {
    $uvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
}

if (-not (Test-Path -LiteralPath $uvPath)) {
    throw "uv was not found. Install it from https://docs.astral.sh/uv/"
}

$sourceVersion = (& $uvPath run --no-sync python -c "from easytype_app import APP_VERSION; print(APP_VERSION)").Trim()

& $uvPath sync --group build
if ($LASTEXITCODE -ne 0) {
    throw "uv sync failed with exit code $LASTEXITCODE."
}

& $uvPath run --group build pyinstaller --noconfirm --clean EasyType.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$exePath = Join-Path $PSScriptRoot "dist\EasyType-$sourceVersion.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "The build finished without creating dist\EasyType-$sourceVersion.exe."
}

$exeVersion = (Get-Item -LiteralPath $exePath).VersionInfo.FileVersion
if ($exeVersion -ne $sourceVersion) {
    throw "Version mismatch: source=$sourceVersion, exe=$exeVersion"
}

Write-Host ""
Write-Host "EasyType $exeVersion EXE built successfully: $exePath"
