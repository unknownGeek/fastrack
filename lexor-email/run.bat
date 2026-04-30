@echo off
REM ====================================================================
REM lexor-email - one-click runner (Windows)
REM   run.bat                preview-prompt + real send
REM   run.bat --dry-run      preview only
REM   run.bat --to a@b.com   ad-hoc recipient
REM ====================================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "VENV_DIR=.venv"
set "REQ_FILE=requirements.txt"
set "ENV_FILE=.env"
set "ENV_EXAMPLE=.env.example"
set "CONFIG_FILE=config.yaml"

REM --- pick python -----------------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if "%PY%"=="" (
    where python >nul 2>&1 && set "PY=python"
)
if "%PY%"=="" (
    echo [fail] Python 3.8+ is required but was not found on PATH.
    exit /b 1
)

REM --- venv ------------------------------------------------------------
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [run] creating virtualenv in %VENV_DIR% ...
    %PY% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [fail] failed to create venv
        exit /b 1
    )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

REM --- dependencies (always sync; cheap on Windows) --------------------
set "STAMP=%VENV_DIR%\.requirements.stamp"
set "NEEDS_INSTALL=1"
if exist "%STAMP%" (
    for %%I in ("%REQ_FILE%") do set "REQ_DATE=%%~tI"
    for %%I in ("%STAMP%") do set "STAMP_DATE=%%~tI"
    if "!REQ_DATE!"=="!STAMP_DATE!" set "NEEDS_INSTALL=0"
)
if "%NEEDS_INSTALL%"=="1" (
    echo [run] installing dependencies from %REQ_FILE% ...
    "%VENV_PIP%" install --upgrade pip --quiet
    "%VENV_PIP%" install -r "%REQ_FILE%" --quiet
    if errorlevel 1 (
        echo [fail] dependency install failed
        exit /b 1
    )
    echo. > "%STAMP%"
)

REM --- preflight: .env -------------------------------------------------
if not exist "%ENV_FILE%" (
    if exist "%ENV_EXAMPLE%" (
        echo [warn] %ENV_FILE% not found.
        set /p REPLY="      Create one from %ENV_EXAMPLE% now? [Y/n] "
        if "!REPLY!"=="" set "REPLY=Y"
        if /i "!REPLY!"=="Y" (
            copy /Y "%ENV_EXAMPLE%" "%ENV_FILE%" >nul
            echo [ok] %ENV_FILE% created. Open it, set GMAIL_SENDER_EMAIL + GMAIL_APP_PASSWORD, then re-run.
            exit /b 0
        )
    )
)

REM --- preflight: config + body + attachments --------------------------
if not exist "%CONFIG_FILE%" (
    echo [fail] %CONFIG_FILE% missing. See README.md.
    exit /b 1
)

"%VENV_PY%" -c "import yaml,pathlib,sys; cfg=yaml.safe_load(open('config.yaml')) or {}; e=cfg.get('email') or {}; bp=e.get('body_markdown_path','email_body.md'); ats=e.get('attachments') or []; ats=[ats] if isinstance(ats,str) else ats; m=[a for a in ats if not pathlib.Path(a).is_file()]; sys.exit(0) if pathlib.Path(bp).is_file() and not m else (print('[fail] body or attachment missing:', bp, m) or sys.exit(1))"
if errorlevel 1 exit /b 1

REM --- run -------------------------------------------------------------
echo [run] launching send_email.py %*
echo ----------------------------------------------------------------------
"%VENV_PY%" send_email.py %*
exit /b %ERRORLEVEL%
