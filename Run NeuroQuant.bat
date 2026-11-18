@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo NeuroQuant development environment was not found: venv\Scripts\python.exe
    echo Create the project environment and install requirements.txt first.
    pause
    exit /b 1
)

set "QT_BIN=%~dp0venv\Lib\site-packages\PySide6"
set "SHIBOKEN_BIN=%~dp0venv\Lib\site-packages\shiboken6"
if exist "%QT_BIN%" set "PATH=%QT_BIN%;%QT_BIN%\plugins\platforms;%SHIBOKEN_BIN%;%PATH%"

"%PYTHON%" "%~dp0main.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo NeuroQuant exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
pause
exit /b 1
