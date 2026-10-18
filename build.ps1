$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 with Tkinter is required.' }
}
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $taskPython -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
$taskResources = Join-Path $PSScriptRoot 'resources'
$taskIcon = Join-Path $taskResources 'handoff.ico'
& $taskPython -m PyInstaller --noconfirm --onefile --windowed --name CodexHandoff --icon $taskIcon --add-data ($taskResources + ';resources') handoff.py
if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
Write-Output 'Ready: dist/CodexHandoff.exe'
