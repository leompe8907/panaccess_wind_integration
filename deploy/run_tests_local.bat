@echo off
REM Corre la suite de tests de Django en local (Windows) y muestra el
REM resultado de cada test individual.
REM
REM Uso (desde la raiz del repo, D:\Back-Wind-V2):
REM   deploy\run_tests_local.bat
REM   deploy\run_tests_local.bat wind.tests.test_auth
REM
REM Requisitos en local: Postgres corriendo y accesible con los datos del
REM .env, y el usuario de DB con permiso CREATEDB (Django crea una base de
REM test aparte, no toca la real). Si no hay Redis local corriendo, los tests
REM que usan el lock de registro (SubscriberRegistrationTestCase) van a fallar
REM con "Connection refused" -- es esperado, no es un bug nuevo.
REM
REM Variables opcionales:
REM   set KEEPDB=1   -> no borra la base de test al final (mas rapido en corridas repetidas)

cd /d "%~dp0\.."
call env\Scripts\activate.bat

set ARGS=%*
if "%ARGS%"=="" set ARGS=wind

set EXTRA=
if "%KEEPDB%"=="1" set EXTRA=--keepdb

echo === manage.py check ===
python manage.py check
if errorlevel 1 goto :end

echo === Corriendo tests: %ARGS% ===
python manage.py test %ARGS% --verbosity=2 %EXTRA%

:end
