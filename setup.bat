@echo off
echo ================================================
echo  WCS - Wireless Charging Station Setup
echo ================================================
echo.

REM Check that Python 3.10.11 is available
python --version 2>NUL
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found. Please install Python 3.10.11 from https://www.python.org/downloads/release/python-31011/
    pause
    exit /b 1
)

echo Creating virtual environment...
python -m venv .venv
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

echo Activating virtual environment...
call .venv\Scripts\activate.bat

echo Upgrading pip...
python -m pip install --upgrade pip

echo Installing dependencies...
pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo ================================================
echo  Setup complete! Run the project with:
echo    .venv\Scripts\activate.bat
echo    python sound_manager.py
echo ================================================
pause
