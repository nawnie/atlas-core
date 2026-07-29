@echo off
setlocal
cd /d "%~dp0"
if not exist "config\atlas-automation.json" (
  echo Atlas automation is not configured.
  echo Copy config\atlas-automation.example.json to config\atlas-automation.json,
  echo then review every tenant, connector, source, inbox, and retention setting.
  exit /b 2
)
py -3.12 scripts\run_automation.py --prompt-passphrase
endlocal
