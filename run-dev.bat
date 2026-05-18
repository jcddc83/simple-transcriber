@echo off
REM Run Transcriber from source (without building an .exe).
REM Useful for testing before committing to a full PyInstaller build.

python -m pip install -r requirements.txt
python transcribe.py
