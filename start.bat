@echo off
echo Starte FundsprospectPilot...

REM In Projektverzeichnis wechseln
cd /d "%~dp0"

REM Python pruefen
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo FEHLER: Python nicht gefunden! Bitte setup.bat ausfuehren.
    pause
    exit /b 1
)

REM .env pruefen
if not exist .env (
    echo FEHLER: .env Datei fehlt! Bitte setup.bat ausfuehren.
    pause
    exit /b 1
)

REM App starten
python src/app.py

if %errorlevel% neq 0 (
    echo.
    echo FEHLER beim Starten. Details im Fehlerprotokoll oben.
    pause
)
