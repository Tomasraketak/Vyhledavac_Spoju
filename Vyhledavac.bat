@echo off
rem ---------------------------------------------------------------------------
rem  Vyhledavac spoju - spusteni na Windows jednim kliknutim.
rem
rem  Bez parametru otevre webove rozhrani v prohlizeci. S parametry se chova jako
rem  prikazova radka, takze jde volat i takto:
rem      Vyhledavac.bat Praha Brno --sort price
rem
rem  Pri prvnim spusteni si vedle sebe vytvori virtualni prostredi (.venv)
rem  a doinstaluje knihovny. Pri kazdem spusteni stahne nejnovejsi verzi
rem  (git pull) a doinstaluje, co pribylo - aktualizace = dvojklik.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
rem Soubor je schvalne jen v ASCII (bez hacku a carek) a bez "chcp 65001":
rem cmd.exe v kodove strance UTF-8 spatne cte bloky v zavorkach s ceskymi
rem znaky a okno se zavre driv, nez se program spusti.
title Vyhledavac spoju
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "VENV=%~dp0.venv"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PYC=%VENV%\Scripts\python.exe"
set "HOTOVO=%VENV%\.instalace-hotova"

rem Aktualizace: je-li program stazeny pres git, stahne nejnovejsi verzi.
rem Bez internetu nebo s vlastnimi upravami se nic nestane - spusti se,
rem co je na disku. Vypnout: soubor "bez-aktualizace.txt" vedle tohoto souboru.
rem Cely blok se nacte najednou: git pull muze prepsat i tento soubor, proto
rem se pak spusti jeho nova verze (uz bez dalsi aktualizace) a tahle skonci.
if not defined VYH_AKTUALIZOVANO if exist "%~dp0.git" if not exist "%~dp0bez-aktualizace.txt" (
    where git >nul 2>&1 && (
        echo   Hledam novou verzi programu ...
        git -C "%~dp0." pull --ff-only --quiet >nul 2>&1
        set "VYH_AKTUALIZOVANO=1"
        call "%~f0" %*
        exit /b
    )
)

if not exist "%PYC%" goto instalace
if not exist "%HOTOVO%" goto instalace
rem Po aktualizaci (git pull) se mohl zmenit seznam knihoven - doinstalovat.
fc /b "%~dp0requirements.txt" "%VENV%\requirements.txt" >nul 2>&1
if errorlevel 1 goto aktualizace
goto spustit


:aktualizace
echo.
echo   Program byl aktualizovan - doinstalovavam nove knihovny ...
"%PYC%" -m pip install --quiet --disable-pip-version-check --upgrade -r "%~dp0requirements.txt"
if errorlevel 1 goto chybaPip
copy /y "%~dp0requirements.txt" "%VENV%\requirements.txt" >nul
goto spustit


rem ===========================================================================
rem  Prvni spusteni
rem ===========================================================================
:instalace
echo.
echo   Vyhledavac spoju
echo   ================
echo.
echo   Prvni spusteni - pripravuji prostredi. Chvili to potrva,
echo   priste se program spusti rovnou.
echo.

call :najdiPython
if errorlevel 1 goto chybiPython

if not exist "%PYC%" (
    echo   [1/3] Vytvarim virtualni prostredi ve slozce .venv ...
    %PYTHON% -m venv "%VENV%"
    if errorlevel 1 goto chybaVenv
)
if not exist "%PYC%" goto chybaVenv

echo   [2/3] Instaluji knihovny ...
"%PYC%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%PYC%" -m pip install --quiet --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 goto chybaPip

echo.
echo   [3/3] Volitelne: prohlizec pro Ceske drahy.
echo.
echo         Bez nej funguje RegioJet, FlixBus a OBB - tedy vetsina spoju
echo         i cen. Ceske drahy a ZSSK potrebuji navic Playwright
echo         s Chromiem, coz je zhruba 150 MB ke stazeni.
echo.
set "CDANO="
set /p "CDANO=        Stahnout ted? [a/N]: "
if /i "%CDANO%"=="a" goto playwright
if /i "%CDANO%"=="ano" goto playwright
if /i "%CDANO%"=="y" goto playwright
echo.
echo         Preskakuji. Doinstalovat jde kdykoli: smazte soubor
echo         .venv\.instalace-hotova a spustte tento program znovu.
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
echo   Hotovo, spoustim.


rem ===========================================================================
rem  Spusteni
rem ===========================================================================
:spustit
if not "%~1"=="" goto radka

rem Rychla kontrola, nez okno konzole zmizi - at pripadna chyba neni nema.
"%PYC%" -c "import httpx, vyhledavac.web.server" 2>&1
if errorlevel 1 goto chybaImport

rem Webove rozhrani: maly server na tomto pocitaci + okno prohlizece.
rem pythonw.exe neotevira cerne okno konzole; server se sam ukonci,
rem kdyz stranku v prohlizeci zavrete. Puvodni okno (tkinter) dal jde
rem spustit prikazem:  .venv\Scripts\python.exe -m vyhledavac.gui
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
rem  Hledani Pythonu - nastavi PYTHON, nebo vrati errorlevel 1
rem ===========================================================================
:najdiPython
rem Windows Python Launcher je nejspolehlivejsi, umi si sam vybrat verzi.
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=py -3"
    exit /b 0
)
rem Bez launcheru zkusime python z PATH. Pozor: zastupce z Microsoft Store
rem se tvari jako python.exe, ale jen otevre obchod - proto ten testovaci import.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=python"
    exit /b 0
)
exit /b 1


rem ===========================================================================
rem  Chybove hlasky - at uzivatel vi, co s tim
rem ===========================================================================
:chybiPython
echo.
echo   CHYBA: Nenasel jsem Python 3.10 nebo novejsi.
echo.
echo   Stahnete ho z https://www.python.org/downloads/
echo   Pri instalaci zaskrtnete "Add python.exe to PATH" a nechte
echo   zapnute "tcl/tk and IDLE" - hodi se pro starsi okenni rozhrani.
echo.
echo   Verzi z Microsoft Store program nepouzije, ta byva osekana.
echo.
pause
exit /b 1

:chybaVenv
echo.
echo   CHYBA: Nepodarilo se vytvorit virtualni prostredi ve slozce .venv
echo.
echo   Zkuste slozku .venv smazat a spustit znovu. Lezi-li program
echo   v OneDrive nebo v Program Files, presunte ho jinam - tam
echo   Windows casto zakazuji zapis.
echo.
pause
exit /b 1

:chybaPip
echo.
echo   CHYBA: Nepodarilo se stahnout knihovny.
echo   Nejcasteji za to muze chybejici pripojeni k internetu
echo   nebo firemni proxy. Zkuste to prosim znovu.
echo.
pause
exit /b 1

:chybaPlaywright
echo.
echo   Playwright se nenainstaloval. Nevadi - program pobezi dal,
echo   jen bez Ceskych drah a ZSSK. Zkusit znovu jde tak, ze smazete
echo   soubor .venv\.instalace-hotova a spustite tento program.
echo.
pause
goto hotovo

:chybaImport
echo.
echo   CHYBA: Prostredi je poskozene - vypis je vyse.
echo   Smazte celou slozku .venv a spustte tento program znovu,
echo   nainstaluje se nacisto.
echo.
pause
exit /b 1

:chybaBehu
echo.
echo   Program skoncil chybou. Vypis je vyse.
echo.
pause
exit /b 1

:konec
endlocal
exit /b 0
