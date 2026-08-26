@echo off
REM -- ConwayGolf POY Standing Poller ---------------------------------------
REM Polls Bryan's standing in the KGA's John C. Owens Player of the Year race
REM (Men's Overall) via a real GolfGenius season-points widget URL -- see
REM tools/update_poy_standing.py header for details.

cd /d C:\Users\frost\ConwayGolf
"C:\Users\frost\AppData\Local\Python\pythoncore-3.14-64\python.exe" tools\update_poy_standing.py
