$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed' }
}
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $buildPython -X utf8 -m pip install -r requirements-test.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
$workspace = Split-Path -Parent $PSScriptRoot
$verification = Join-Path $workspace 'work\build-verification'
$testTemp = Join-Path $verification 'Temp'
New-Item -ItemType Directory -Path $testTemp -Force | Out-Null
$previousEnvironment = @{}
foreach ($name in @('LOCALAPPDATA', 'TEMP', 'TMP', 'PYTHONUTF8')) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:LOCALAPPDATA = Join-Path $verification 'LocalAppData'
    $env:TEMP = $testTemp
    $env:TMP = $testTemp
    $env:PYTHONUTF8 = '1'
    & $buildPython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
} finally {
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
$buildWork = Join-Path $workspace 'work\pyinstaller'
$buildDist = Join-Path $workspace 'work\dist'
& $buildPython -m PyInstaller --noconfirm --clean --workpath $buildWork --distpath $buildDist LCSC3D.spec
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
$latestOutput = Join-Path (Split-Path -Parent $PSScriptRoot) 'outputs'
New-Item -ItemType Directory -Path $latestOutput -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $buildDist 'LCSC3D.exe') -Destination (Join-Path $latestOutput 'LCSC3D.exe') -Force
Write-Host "Done: latest EXE is in $latestOutput\LCSC3D.exe"
