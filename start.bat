@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === A.S Finances CRM ===
echo.
where py >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install it from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
if not exist venv (
  echo First run: setting things up, this takes a minute...
  py -m venv venv
)
call venv\Scripts\activate
set DEBUG=1
pip install -r requirements.txt -q
python manage.py migrate -v0
python manage.py createcachetable >nul 2>nul
if not exist .admin_created (
  echo.
  echo Create your admin login. Pick a LONG password ^(10+ characters^).
  python manage.py createsuperuser
  if errorlevel 1 ( pause & exit /b 1 )
  echo done> .admin_created
)
if not exist .revital_set (
  echo.
  echo Set a login password for Revital ^(username: revital^). Pick a LONG password ^(10+ characters^).
  python manage.py changepassword revital
  if not errorlevel 1 echo done> .revital_set
)
start "" http://127.0.0.1:8000
echo.
echo CRM is running. Keep this window open. Close it to stop the CRM.
python manage.py runserver 127.0.0.1:8000
pause
