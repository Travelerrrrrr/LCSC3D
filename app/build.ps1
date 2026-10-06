$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed' }
}
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $buildPython -X utf8 -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $buildPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
& $buildPython -m PyInstaller --noconfirm --clean LCSC3D.spec
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
Write-Host 'Done: see dist\LCSC3D.exe'
