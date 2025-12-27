@echo off
SET VENV_DIR=venv

IF NOT EXIST "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
)

"%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt

"%VENV_DIR%\Scripts\python.exe" -m rewrite.frontend.server