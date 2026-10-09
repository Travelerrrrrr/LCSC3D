$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed' }
}
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $buildPython -X utf8 -m pip install -r requirements-test.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $buildPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
& $buildPython -m PyInstaller --noconfirm --clean LCSC3D.spec
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
$latestOutput = Join-Path (Split-Path -Parent $PSScriptRoot) 'outputs'
New-Item -ItemType Directory -Path $latestOutput -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'dist\LCSC3D.exe') -Destination (Join-Path $latestOutput 'LCSC3D.exe') -Force
Write-Host "Done: latest EXE is in $latestOutput\LCSC3D.exe"
