@echo off
REM -- ConwayGolf Live Match Tracker -----------------------------------------
REM Polls GolfGenius for Bryan Conway's current match and updates the live
REM banner (both git remotes) when something real changes. No-ops past the
REM STOP_DATE set inside tools/live_match_tracker.py -- update that (and this
REM task's schedule) per new tournament.

cd /d C:\Users\frost\ConwayGolf
python tools\live_match_tracker.py
