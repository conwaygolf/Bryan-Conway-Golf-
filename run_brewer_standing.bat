@echo off
REM -- ConwayGolf Brewer Standing Poller ---------------------------------------
REM Polls Bryan's season standing in the Play Golf Lex AM Tour Brewer flight
REM (same weekly Tates Creek tour as Round 1, Aug 22 2026) via a real GolfGenius
REM season-points widget URL -- see tools/update_brewer_standing.py header for
REM why this exists (no live single-round leaderboard URL was discoverable).

cd /d C:\Users\frost\ConwayGolf
"C:\Users\frost\AppData\Local\Python\pythoncore-3.14-64\python.exe" tools\update_brewer_standing.py
