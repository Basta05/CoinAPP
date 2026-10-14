@echo off
cd /d "%~dp0"
if not exist .venv (
    echo Vytvarim virtualni prostredi...
    python -m venv .venv || (echo Python neni nainstalovany nebo neni v PATH & pause & exit /b 1)
    .venv\Scripts\python -m pip install --upgrade pip
)
echo Kontroluji balicky...
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)
if not exist .env copy .env.example .env >nul
.venv\Scripts\python.exe app.py
if errorlevel 1 pause
