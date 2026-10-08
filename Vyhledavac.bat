@echo off
rem ---------------------------------------------------------------------------
rem  Vyhledávač spojů - spuštění na Windows jedním kliknutím.
rem
rem  Bez parametrů otevře webové rozhraní v prohlížeči. S parametry se chová jako
rem  příkazová řádka, takže jde volat i takto:
rem      Vyhledavac.bat Praha Brno --sort price
rem
rem  Při prvním spuštění si vedle sebe vytvoří virtuální prostředí (.venv)
rem  a doinstaluje knihovny. Příště se spustí rovnou.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Vyhledávač spojů
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "VENV=%~dp0.venv"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PYC=%VENV%\Scripts\python.exe"
set "HOTOVO=%VENV%\.instalace-hotova"

if not exist "%PYC%" goto instalace
if not exist "%HOTOVO%" goto instalace
rem Po aktualizaci (git pull) se mohl změnit seznam knihoven - doinstalovat.
fc /b "%~dp0requirements.txt" "%VENV%\requirements.txt" >nul 2>&1
if errorlevel 1 goto aktualizace
goto spustit


:aktualizace
echo.
echo   Program byl aktualizován - doinstalovávám nové knihovny ...
"%PYC%" -m pip install --quiet --disable-pip-version-check --upgrade -r "%~dp0requirements.txt"
if errorlevel 1 goto chybaPip
copy /y "%~dp0requirements.txt" "%VENV%\requirements.txt" >nul
goto spustit


rem ===========================================================================
rem  První spuštění
rem ===========================================================================
:instalace
echo.
echo   Vyhledávač spojů
echo   ================
echo.
echo   První spuštění - připravuji prostředí. Chvíli to potrvá,
echo   příště se program spustí rovnou.
echo.

call :najdiPython
if errorlevel 1 goto chybiPython

if not exist "%PYC%" (
    echo   [1/3] Vytvářím virtuální prostředí ve složce .venv ...
    %PYTHON% -m venv "%VENV%"
    if errorlevel 1 goto chybaVenv
)
if not exist "%PYC%" goto chybaVenv

echo   [2/3] Instaluji knihovny ...
"%PYC%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%PYC%" -m pip install --quiet --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 goto chybaPip

echo.
echo   [3/3] Volitelně: prohlížeč pro České dráhy.
echo.
echo         Bez něj funguje RegioJet, FlixBus a ÖBB - tedy většina spojů
echo         i cen. České dráhy a ZSSK potřebují navíc Playwright
echo         s Chromiem, což je zhruba 150 MB ke stažení.
echo.
set "CDANO="
set /p "CDANO=        Stáhnout teď? [a/N]: "
if /i "%CDANO%"=="a" goto playwright
if /i "%CDANO%"=="ano" goto playwright
if /i "%CDANO%"=="y" goto playwright
echo.
echo         Přeskakuji. Doinstalovat jde kdykoli: smažte soubor
echo         .venv\.instalace-hotova a spusťte tento program znovu.
goto hotovo

:playwright
echo         Instaluji Playwright ...
"%PYC%" -m pip install --quiet --disable-pip-version-check playwright
if errorlevel 1 goto chybaPlaywright
echo         Stahuji Chromium ...
"%PYC%" -m playwright install chromium
if errorlevel 1 goto chybaPlaywright

:hotovo
echo hotovo> "%HOTOVO%"
copy /y "%~dp0requirements.txt" "%VENV%\requirements.txt" >nul
echo.
echo   Hotovo, spouštím.


rem ===========================================================================
rem  Spuštění
rem ===========================================================================
:spustit
if not "%~1"=="" goto radka

rem Rychlá kontrola, než okno konzole zmizí - ať případná chyba není němá.
"%PYC%" -c "import httpx, vyhledavac.web.server" 2>&1
if errorlevel 1 goto chybaImport

rem Webové rozhraní: malý server na tomto počítači + okno prohlížeče.
rem pythonw.exe neotevírá černé okno konzole; server se sám ukončí,
rem když stránku v prohlížeči zavřete. Původní okno (tkinter) dál jde
rem spustit příkazem:  .venv\Scripts\python.exe -m vyhledavac.gui
if exist "%PYW%" (
    start "" "%PYW%" -m vyhledavac.web
    goto konec
)
"%PYC%" -m vyhledavac.web
if errorlevel 1 goto chybaBehu
goto konec

:radka
"%PYC%" -m vyhledavac %*
if errorlevel 1 (
    echo.
    pause
)
goto konec


rem ===========================================================================
rem  Hledání Pythonu - nastaví PYTHON, nebo vrátí errorlevel 1
rem ===========================================================================
:najdiPython
rem Windows Python Launcher je nejspolehlivější, umí si sám vybrat verzi.
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=py -3"
    exit /b 0
)
rem Bez launcheru zkusíme python z PATH. Pozor: zástupce z Microsoft Store
rem se tváří jako python.exe, ale jen otevře obchod - proto ten testovací import.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=python"
    exit /b 0
)
exit /b 1


rem ===========================================================================
rem  Chybové hlášky - ať uživatel ví, co s tím
rem ===========================================================================
:chybiPython
echo.
echo   CHYBA: Nenašel jsem Python 3.10 nebo novější.
echo.
echo   Stáhněte ho z https://www.python.org/downloads/
echo   Při instalaci zaškrtněte "Add python.exe to PATH" a nechte
echo   zapnuté "tcl/tk and IDLE" - hodí se pro starší okenní rozhraní.
echo.
echo   Verzi z Microsoft Store program nepoužije, ta bývá osekaná.
echo.
pause
exit /b 1

:chybaVenv
echo.
echo   CHYBA: Nepodařilo se vytvořit virtuální prostředí ve složce .venv
echo.
echo   Zkuste složku .venv smazat a spustit znovu. Leží-li program
echo   v OneDrive nebo v Program Files, přesuňte ho jinam - tam
echo   Windows často zakazují zápis.
echo.
pause
exit /b 1

:chybaPip
echo.
echo   CHYBA: Nepodařilo se stáhnout knihovny.
echo   Nejčastěji za to může chybějící připojení k internetu
echo   nebo firemní proxy. Zkuste to prosím znovu.
echo.
pause
exit /b 1

:chybaPlaywright
echo.
echo   Playwright se nenainstaloval. Nevadí - program poběží dál,
echo   jen bez Českých drah a ZSSK. Zkusit znovu jde tak, že smažete
echo   soubor .venv\.instalace-hotova a spustíte tento program.
echo.
pause
goto hotovo

:chybaImport
echo.
echo   CHYBA: Prostředí je poškozené - výpis je výše.
echo   Smažte celou složku .venv a spusťte tento program znovu,
echo   nainstaluje se načisto.
echo.
pause
exit /b 1

:chybaBehu
echo.
echo   Program skončil chybou. Výpis je výše.
echo.
pause
exit /b 1

:konec
endlocal
exit /b 0
