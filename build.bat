@echo off
REM Build SimpleTranscriber.exe with PyInstaller.
REM Auto-downloads ffmpeg (for audio processing) and the WebView2 bootstrapper
REM (for the installer) on first run.

setlocal

echo Installing/refreshing dependencies...
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto :err

if not exist ffmpeg.exe (
  echo Downloading ffmpeg...
  curl -L "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -o ffmpeg-temp.zip
  if errorlevel 1 goto :err
  powershell -Command "Expand-Archive -Path ffmpeg-temp.zip -DestinationPath ffmpeg-temp -Force"
  if errorlevel 1 goto :err
  powershell -Command "Copy-Item (Get-ChildItem 'ffmpeg-temp\*\bin\ffmpeg.exe' | Select-Object -First 1).FullName ffmpeg.exe"
  powershell -Command "Copy-Item (Get-ChildItem 'ffmpeg-temp\*\bin\ffprobe.exe' | Select-Object -First 1).FullName ffprobe.exe"
  rmdir /s /q ffmpeg-temp
  del ffmpeg-temp.zip
  echo ffmpeg ready.
)

if not exist deno.exe (
  echo Downloading Deno JS runtime...
  curl -L "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" -o deno-temp.zip
  if errorlevel 1 goto :err
  powershell -Command "Expand-Archive -Path deno-temp.zip -DestinationPath deno-temp -Force"
  if errorlevel 1 goto :err
  powershell -Command "Copy-Item (Get-ChildItem 'deno-temp\deno.exe' -Recurse | Select-Object -First 1).FullName deno.exe"
  rmdir /s /q deno-temp
  del deno-temp.zip
  echo Deno ready.
)

if not exist MicrosoftEdgeWebview2Setup.exe (
  echo Downloading WebView2 bootstrapper...
  curl -L "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -o MicrosoftEdgeWebview2Setup.exe
  if errorlevel 1 goto :err
  echo WebView2 bootstrapper ready.
)

echo Building SimpleTranscriber.exe...
python -m PyInstaller --onefile --windowed --name SimpleTranscriber ^
  --icon=app.ico ^
  --version-file version.txt ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --add-binary "ffmpeg.exe;." ^
  --add-binary "ffprobe.exe;." ^
  --add-binary "deno.exe;." ^
  --collect-all webview ^
  --hidden-import webview.platforms.edgechromium ^
  transcribe.py
if errorlevel 1 goto :err

echo.
echo Done. The executable is at: dist\SimpleTranscriber.exe
echo Double-click it to launch. Config (API keys) will be saved next to the .exe on first run.
echo.
echo To build the installer, run: iscc installer.iss
goto :eof

:err
echo.
echo Build failed. Scroll up for the first error message.
exit /b 1
