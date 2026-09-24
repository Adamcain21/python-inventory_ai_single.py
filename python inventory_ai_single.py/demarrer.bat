@echo off
rem Inventory AI - starts the server for the restaurant (this PC + tablets/phones on the same Wi-Fi)
cd /d "%~dp0"
rem backup of the database at every start (folder "backups")
powershell -NoProfile -Command "New-Item -ItemType Directory -Force backups | Out-Null; if (Test-Path inventory.db) { Copy-Item inventory.db ('backups\inventory-' + (Get-Date -Format yyyyMMdd-HHmm) + '.db') }"
echo.
echo  Inventory AI
echo  - On this PC:        http://localhost:8000
echo  - Tablets / phones:  http://ADDRESS:8000  with one of these addresses:
ipconfig | findstr /c:"IPv4"
echo.
echo  Keep this window open during service. Close it to stop.
echo.
python -m uvicorn api:app --host 0.0.0.0 --port 8000
pause
