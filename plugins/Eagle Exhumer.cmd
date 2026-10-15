@echo off
rem Eagle Exhumer stand-alone launcher: finds KiCad's Python and starts the intro window.
set "PY="
for /d %%D in ("%LOCALAPPDATA%\Programs\KiCad\*") do if exist "%%D\bin\pythonw.exe" set "PY=%%D\bin\pythonw.exe"
for /d %%D in ("%ProgramFiles%\KiCad\*") do if exist "%%D\bin\pythonw.exe" set "PY=%%D\bin\pythonw.exe"
if not defined PY (
  echo KiCad 10 was not found. Install KiCad first.
  pause
  exit /b 1
)
start "" "%PY%" "%~dp0exhumer_launcher.py"
