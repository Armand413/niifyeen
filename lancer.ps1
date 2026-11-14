# Lance NiifYeen avec la boite de reception ouverte aux telephones du reseau local.
# L'application principale reste sur 127.0.0.1:8000 (cet ordinateur seulement).
$env:NIIFYEEN_INBOX_HOST = "0.0.0.0"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
