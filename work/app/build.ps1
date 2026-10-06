param(
    [string]$QtPrefix = $env:LCSC3D_QT_PREFIX,
    [string]$CompilerPrefix = $env:LCSC3D_COMPILER_PREFIX
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed' }
}
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $buildPython -X utf8 -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
if ($QtPrefix -and $CompilerPrefix) {
    & $buildPython native/build_native.py --qt-prefix $QtPrefix --compiler-prefix $CompilerPrefix
    if ($LASTEXITCODE -ne 0) { throw 'Altium converter build failed' }
} elseif (-not (Test-Path -LiteralPath 'native\runtime\lcsc-altium.exe')) {
    throw 'Supply -QtPrefix and -CompilerPrefix to build the bundled Altium converter. See README.md.'
}
& $buildPython native/build_native.py --verify
if ($LASTEXITCODE -ne 0) { throw 'Altium converter verification failed' }
$env:LCSC3D_REQUIRE_NATIVE_TESTS = '1'
& $buildPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
& $buildPython -m PyInstaller --noconfirm --clean LCSC3D.spec
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
Write-Host 'Done: dist\LCSC3D-Portable-v1.1.1.exe'
