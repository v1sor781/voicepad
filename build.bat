@echo off
cd /d "%~dp0"
echo Installing dependencies...
py -m pip install --upgrade pip
py -m pip install customtkinter sounddevice soundfile numpy requests keyboard pyinstaller
echo Building VoicePad.exe...
py -m PyInstaller --noconsole --onefile --name VoicePad --collect-all customtkinter --collect-all _sounddevice_data --collect-all _soundfile_data voicepad.py
echo.
echo Done: dist\VoicePad.exe
pause
