@echo off
REM -- ConwayGolf Live Senior Open Tracker -----------------------------------
REM Polls GolfGenius's stroke-play leaderboard for Bryan Conway's live
REM position/score at the 27th Kentucky Senior Open and updates the live
REM banner (both git remotes) when something real changes. No-ops past the
REM STOP_DATE set inside tools/live_senior_open_tracker.py -- update that
REM (and this task's schedule) for any future stroke-play event.

cd /d C:\Users\frost\ConwayGolf
"C:\Users\frost\AppData\Local\Python\pythoncore-3.14-64\python.exe" tools\live_senior_open_tracker.py
