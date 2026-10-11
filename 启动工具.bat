@echo off
setlocal
cd /d "%~dp0"
title OSM Building Tool

set "PY_EXE="
set "PY_ARGS="
set "CODEX_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEX_PY%" set "PY_EXE=%CODEX_PY%"
if defined PY_EXE goto python_found

py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto try_python
set "PY_EXE=py"
set "PY_ARGS=-3"
goto python_found

:try_python
python -c "import sys" >nul 2>nul
if errorlevel 1 goto no_python
set "PY_EXE=python"

:python_found
if "%~1"=="--check" (
  echo Python: %PY_EXE% %PY_ARGS%
  "%PY_EXE%" %PY_ARGS% --version
  exit /b %errorlevel%
)

"%PY_EXE%" %PY_ARGS% -c "import PIL, numpy" >nul 2>nul
if errorlevel 1 goto install_dependencies
goto launch

:install_dependencies
echo Installing required components. This is only needed the first time...
"%PY_EXE%" %PY_ARGS% -m pip install -r requirements.txt
if errorlevel 1 goto failed

:launch
"%PY_EXE%" %PY_ARGS% gui.py
if errorlevel 1 goto failed
exit /b 0

:no_python
echo.
echo Python was not found.
echo Install Python 3.11 or newer from https://www.python.org/downloads/
echo During installation, enable "Add python.exe to PATH".
echo.
pause
exit /b 1

:failed
echo.
echo The tool could not start. The error message is shown above.
echo You can send a screenshot of this window for diagnosis.
echo.
pause
exit /b 1
