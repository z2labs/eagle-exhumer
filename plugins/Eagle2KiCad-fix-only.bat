@echo off
rem Eagle Exhumer - fix + verify only, for a project that was already imported from Eagle in KiCad.
rem Drag the KiCad project folder onto this file (optionally: --eagle-sch x.sch --eagle-brd x.brd).
"C:\Program Files\KiCad\10.0\bin\python.exe" "%~dp0eagle2kicad_fix.py" %*
pause
