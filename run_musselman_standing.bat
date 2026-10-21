@echo off
REM -- ConwayGolf Musselman Standing Poller ---------------------------------
REM Polls Bryan's standing in the KGA's Tom Musselman Player of the Year race
REM (Men's Senior) via the same GolfGenius season-points widget as the Owens
REM poller, just a different category filter -- see
REM tools/update_musselman_standing.py header for details.

cd /d C:\Users\frost\ConwayGolf
"C:\Users\frost\AppData\Local\Python\pythoncore-3.14-64\python.exe" tools\update_musselman_standing.py
