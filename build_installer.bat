@echo off
cd /d "%~dp0"
echo Installing dependencies...
py -m pip install --upgrade pip
py -m pip install customtkinter sounddevice soundfile numpy requests keyboard pyinstaller
echo Building VoicePad.exe...
py -m PyInstaller --noconsole --onefile --name VoicePad --collect-all customtkinter --collect-all _sounddevice_data --collect-all _soundfile_data voicepad.py
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" goto noinno
echo Building installer...
"%ISCC%" installer.iss
echo.
echo Done: installer\VoicePad-Setup.exe
pause
exit /b 0
:noinno
echo VoicePad.exe is built (dist folder), but Inno Setup 6 was not found.
echo Install it from https://jrsoftware.org/isdl.php and run this script again.
pause
