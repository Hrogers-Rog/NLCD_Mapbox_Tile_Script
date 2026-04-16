@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: Detect Python
:: ============================================================
set PYTHON=
for %%P in (python python3) do (
    if "!PYTHON!"=="" (
        %%P --version >nul 2>&1 && set PYTHON=%%P
    )
)
if "!PYTHON!"=="" (
    echo ERROR: Python not found on PATH.
    echo Please install Python 3.10+ from https://python.org and try again.
    pause
    exit /b 1
)

:: ============================================================
:: MENU
:: ============================================================
:MENU
cls
echo ============================================================
echo   TERRAIN TILE GENERATOR
echo ============================================================

call :SHOW_TOKEN_STATUS

echo.
echo   1. Generate tile area
echo   2. Generate single tile
echo   3. Setup  ^(libraries + Mapbox token^)
echo   0. Exit
echo.
set /p CHOICE="  Choose: "
if "!CHOICE!"=="1" goto AREA
if "!CHOICE!"=="2" goto SINGLE
if "!CHOICE!"=="3" goto SETUP
if "!CHOICE!"=="0" exit /b 0
goto MENU

:: ============================================================
:: Show token status in menu header
:: ============================================================
:SHOW_TOKEN_STATUS
set TOKEN_STATUS=NOT SET
if exist "config.json" (
    for /f "usebackq delims=" %%L in ("config.json") do (
        set CFG_LINE=%%L
        echo !CFG_LINE! | find /i "mapbox_token" >nul 2>&1
        if !errorlevel!==0 set TOKEN_STATUS=saved in config.json
    )
)
echo   Mapbox token: !TOKEN_STATUS!
goto :EOF

:: ============================================================
:: SETUP - library check + token management
:: ============================================================
:SETUP
cls
echo ============================================================
echo   SETUP
echo ============================================================
echo.
echo  [1/2] Checking required Python libraries...
echo.

for %%L in (numpy scipy requests PIL) do (
    !PYTHON! -c "import %%L" >nul 2>&1
    if !errorlevel!==0 (
        echo        [OK]  %%L
    ) else (
        echo   [MISSING]  %%L  -- installing...
        if "%%L"=="PIL" (
            !PYTHON! -m pip install --quiet Pillow
        ) else (
            !PYTHON! -m pip install --quiet %%L
        )
        !PYTHON! -c "import %%L" >nul 2>&1
        if !errorlevel!==0 (
            echo        [OK]  %%L  ^(installed^)
        ) else (
            echo     [FAIL]  %%L  could not be installed. Run manually: pip install %%L
        )
    )
)
echo.

echo  [2/2] Mapbox token
echo.
echo   Your token is used to fetch terrain height data from Mapbox.
echo   Get one free at: https://account.mapbox.com
echo.

set CURRENT_TOKEN=
set RAW=
if exist "config.json" (
    for /f "tokens=1,2 delims=:, " %%A in ('type "config.json" ^| findstr /i "mapbox_token"') do (
        set RAW=%%B
    )
    if defined RAW (
        set CURRENT_TOKEN=!RAW:"=!
        set CURRENT_TOKEN=!CURRENT_TOKEN: =!
        echo   Current saved token: !CURRENT_TOKEN!
    ) else (
        echo   No token currently saved.
    )
)
echo.
set /p NEW_TOKEN="  Enter new Mapbox token (blank=keep current): "
if "!NEW_TOKEN!"=="" (
    echo   Token unchanged.
) else (
    !PYTHON! -c "import json,pathlib; p=pathlib.Path('config.json'); d=json.loads(p.read_text()) if p.exists() else {}; d['mapbox_token']='!NEW_TOKEN!'; p.write_text(json.dumps(d, indent=2))"
    echo   Token saved to config.json
)

echo.
echo  Setup complete.
pause
goto MENU

:: ============================================================
:: Shared offset subroutine (sets OFFSET_ARGS)
:: ============================================================
:ASK_OFFSET
set OFFSET_ARGS=
set /p NO_OFF="  Disable height offset ramp? [y/N]: "
if /i "!NO_OFF!"=="y" (
    set OFFSET_ARGS=--no-offset
    goto :EOF
)
echo   (Leave blank to use defaults: east=-66  west=-98  max=40m^)
set /p OEX="  Offset east X  [default -66]: "
set /p OWX="  Offset west X  [default -98]: "
set /p OMX="  Offset max metres [default 40]: "
if not "!OEX!"=="" set OFFSET_ARGS=!OFFSET_ARGS! --offset-east-x !OEX!
if not "!OWX!"=="" set OFFSET_ARGS=!OFFSET_ARGS! --offset-west-x !OWX!
if not "!OMX!"=="" set OFFSET_ARGS=!OFFSET_ARGS! --offset-max !OMX!
goto :EOF

:: ============================================================
:: Single tile
:: ============================================================
:SINGLE
cls
echo ============================================================
echo   SINGLE TILE
echo ============================================================
echo.

set "USE_TOKEN="
if exist "config.json" (
    for /f "tokens=1,2 delims=:, " %%A in ('type "config.json" ^| findstr /i "mapbox_token"') do (
        set "RAW=%%B"
        set "USE_TOKEN=!RAW:"=!"
        set "USE_TOKEN=!USE_TOKEN: =!"
    )
)

set /p X="  Tile X: "
set /p Y="  Tile Y: "

set NO_GUTTER=
set VEG_ARG=
set NO_NLCD=
set BLUR_ARG=

set /p NG="  No gutter? Output 512x512 instead of 513x513 [y/N]: "
if /i "!NG!"=="y" set NO_GUTTER=--no-gutter

set /p VEG_VAL="  Veg preset 0-7 (blank=use NLCD): "
if not "!VEG_VAL!"=="" set VEG_ARG=--veg !VEG_VAL!

if "!VEG_VAL!"=="" (
    set /p NN="  Skip NLCD fetch? [y/N]: "
    if /i "!NN!"=="y" set NO_NLCD=--no-nlcd
)

if "!VEG_VAL!"=="" if "!NO_NLCD!"=="" (
    set /p BLUR_VAL="  NLCD blur sigma [default=16.0, 0=off, blank=default]: "
    if not "!BLUR_VAL!"=="" set BLUR_ARG=--nlcd-blur !BLUR_VAL!
)

call :ASK_OFFSET

set TOKEN_ARG=
if defined USE_TOKEN set TOKEN_ARG=--token !USE_TOKEN!

set CMD=!PYTHON! get_tile_4.py !X! !Y! --base-x !X! --base-y !Y! !NO_GUTTER! !VEG_ARG! !NO_NLCD! !BLUR_ARG! !OFFSET_ARGS! !TOKEN_ARG!
echo.
echo ============================================================
echo   Command: !CMD!
echo ============================================================
echo.
set /p CONFIRM="  Run this command? [Y/n]: "
if /i "!CONFIRM!"=="n" goto MENU

echo.
!CMD!
echo.
echo Done.
pause
goto MENU

:: ============================================================
:: Tile area
:: ============================================================
:AREA
cls
echo ============================================================
echo   TILE AREA
echo ============================================================
echo.

set "USE_TOKEN="
if exist "config.json" (
    for /f "tokens=1,2 delims=:, " %%A in ('type "config.json" ^| findstr /i "mapbox_token"') do (
        set "RAW=%%B"
        set "USE_TOKEN=!RAW:"=!"
        set "USE_TOKEN=!USE_TOKEN: =!"
    )
)

set /p X0="  X start (inclusive): "
set /p X1="  X end   (inclusive): "
set /p Y0="  Y start (inclusive): "
set /p Y1="  Y end   (inclusive): "

set NO_GUTTER=
set VEG_ARG=
set NO_NLCD=
set BLUR_ARG=
set WORKERS_ARG=

set /p NG="  No gutter? Output 512x512 instead of 513x513 [y/N]: "
if /i "!NG!"=="y" set NO_GUTTER=--no-gutter

set /p VEG_VAL="  Veg preset 0-7 (blank=use NLCD): "
if not "!VEG_VAL!"=="" set VEG_ARG=--veg !VEG_VAL!

if "!VEG_VAL!"=="" (
    set /p NN="  Skip NLCD fetch? [y/N]: "
    if /i "!NN!"=="y" set NO_NLCD=--no-nlcd
)

if "!VEG_VAL!"=="" if "!NO_NLCD!"=="" (
    set /p BLUR_VAL="  NLCD blur sigma [default=16.0, 0=off, blank=default]: "
    if not "!BLUR_VAL!"=="" set BLUR_ARG=--nlcd-blur !BLUR_VAL!
)

set /p WORKERS_VAL="  Parallel workers [blank=auto]: "
if not "!WORKERS_VAL!"=="" set WORKERS_ARG=--workers !WORKERS_VAL!

call :ASK_OFFSET

set TOKEN_ARG=
if defined USE_TOKEN set TOKEN_ARG=--token !USE_TOKEN!

set CMD=!PYTHON! get_tile_area.py --script get_tile_4.py !X0! !X1! !Y0! !Y1! !NO_GUTTER! !VEG_ARG! !NO_NLCD! !BLUR_ARG! !WORKERS_ARG! !OFFSET_ARGS! !TOKEN_ARG!

echo.
echo ============================================================
echo   Command: !CMD!
echo ============================================================
echo.
set /p CONFIRM="  Run this command? [Y/n]: "
if /i "!CONFIRM!"=="n" goto MENU

echo.
!CMD!
echo.
echo Done.
pause
goto MENU
