@echo off
REM -- ConwayGolf Live Leaderboard Poller ---------------------------------------
REM Admin-driven: only does anything when "Leaderboard enabled" is checked on
REM the LeaderBoard card in /admin, with a tournament code entered. See
REM tools/update_live_leaderboard.py header for how a code becomes real data.
REM Safe to run often -- self-gates and no-ops fast when turned off.

cd /d C:\Users\frost\ConwayGolf
"C:\Users\frost\AppData\Local\Python\pythoncore-3.14-64\python.exe" tools\update_live_leaderboard.py
