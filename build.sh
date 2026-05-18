#!/usr/bin/env bash
# Build SimpleTranscriber.app on macOS.
# Run once after: pip3 install -r requirements.txt
set -e

echo "Installing/refreshing dependencies..."
python3 -m pip install --upgrade -r requirements.txt

# Convert app.ico to app.icns (macOS icon format) if .icns not already present.
if [ ! -f app.icns ] && [ -f app.ico ]; then
  echo "Converting app.ico to app.icns..."
  sips -s format icns app.ico --out app.icns 2>/dev/null || true
fi

echo "Building SimpleTranscriber.app..."
pyinstaller --onefile --windowed --name SimpleTranscriber \
  --icon=app.icns \
  --add-data "templates:templates" \
  --add-data "static:static" \
  --collect-all pywebview \
  --hidden-import pywebview.platforms.cocoa \
  transcribe.py

echo ""
echo "Done. App bundle: dist/SimpleTranscriber.app"
echo "To distribute, wrap in a .dmg:"
echo "  brew install create-dmg"
echo "  create-dmg dist/SimpleTranscriber.app ."
