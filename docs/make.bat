@ECHO OFF

REM Windows command file for Sphinx documentation
REM Mirrors the POSIX Makefile in this directory.

pushd %~dp0

REM Allow override on the command line: `set SPHINXBUILD=...`
if "%SPHINXBUILD%" == "" (
	set SPHINXBUILD=sphinx-build
)
set SOURCEDIR=.
set BUILDDIR=_build

if "%1" == "" goto help

%SPHINXBUILD% >NUL 2>NUL
if errorlevel 9009 (
	echo.
	echo.The 'sphinx-build' command was not found. Make sure you have Sphinx
	echo.installed, then set the SPHINXBUILD environment variable to point
	echo.to the full path of the 'sphinx-build' executable. Alternatively you
	echo.may add the Sphinx directory to PATH.
	echo.
	echo.If you don't have Sphinx installed, install it with:
	echo.
	echo.    pip install -e .[docs]
	echo.
	echo.from the repo root.
	exit /b 1
)

if "%1" == "html-strict" (
	%SPHINXBUILD% -b html -W --keep-going %SOURCEDIR% %BUILDDIR%\html %SPHINXOPTS% %O%
	goto end
)

if "%1" == "clean" (
	if exist %BUILDDIR% rmdir /S /Q %BUILDDIR%
	goto end
)

%SPHINXBUILD% -M %1 %SOURCEDIR% %BUILDDIR% %SPHINXOPTS% %O%
goto end

:help
%SPHINXBUILD% -M help %SOURCEDIR% %BUILDDIR% %SPHINXOPTS% %O%

:end
popd
