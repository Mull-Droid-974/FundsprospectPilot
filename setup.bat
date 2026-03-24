@echo off
echo ================================================
echo  FundsprospectPilot - Einmalige Installation
echo ================================================
echo.

REM Python pruefen
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo FEHLER: Python nicht gefunden!
    echo Bitte Python von https://python.org/downloads herunterladen.
    echo Beim Installieren "Add Python to PATH" ankreuzen!
    pause
    exit /b 1
)

echo Python gefunden. Installiere Abhaengigkeiten...
echo.

REM Pip upgrade
python -m pip install --upgrade pip --quiet

REM Pakete installieren
python -m pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo FEHLER bei der Installation. Bitte Administrator-Rechte pruefen.
    pause
    exit /b 1
)

REM .env erstellen falls nicht vorhanden
if not exist .env (
    copy .env.example .env >nul
    echo.
    echo ================================================
    echo  WICHTIG: Bitte .env Datei oeffnen und
    echo  deinen Anthropic API-Key eintragen!
    echo ================================================
)

REM Verzeichnisse erstellen
if not exist data\input mkdir data\input
if not exist data\output mkdir data\output
if not exist data\prospectus mkdir data\prospectus

echo.
echo ================================================
echo  Installation abgeschlossen!
echo.
echo  Naechste Schritte:
echo  1. .env Datei oeffnen und API-Key eintragen
echo  2. Excel-Datei in data\input\ ablegen
echo  3. start.bat doppelklicken
echo ================================================
echo.
pause
