@echo off
REM -- ConwayGolf Event Result Check -------------------------------------
REM Runs daily. Checks data/schedule.json for events whose end date has
REM passed without a result being fetched yet, tries to auto-find Bryan
REM Conway's final GolfGenius result, and drops a draft in the admin
REM panel for review (never auto-publishes). Emails a reminder either way.

cd /d C:\Users\frost\ConwayGolf
"C:\Users\frost\AppData\Local\Python\pythoncore-3.14-64\python.exe" tools\check_event_completion.py
