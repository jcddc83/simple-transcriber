#!/usr/bin/env bash
# Build SimpleTranscriber.app on macOS.
# Run once after: pip3 install -r requirements.txt
set -e

echo "Installing/refreshing dependencies..."
python3 -m pip install --upgrade -r requirements.txt

# Download the Deno JS runtime (yt-dlp's YouTube extractor needs one) if absent.
if [ ! -f deno ]; then
  echo "Downloading Deno JS runtime..."
  if [ "$(uname -m)" = "arm64" ]; then
    DENO_TARGET="aarch64-apple-darwin"
  else
    DENO_TARGET="x86_64-apple-darwin"
  fi
  curl -L "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_TARGET}.zip" -o deno-temp.zip
  unzip -o deno-temp.zip deno
  chmod +x deno
  rm -f deno-temp.zip
  echo "Deno ready."
fi

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
  --add-binary "deno:." \
  --collect-all pywebview \
  --hidden-import pywebview.platforms.cocoa \
  transcribe.py

echo ""
echo "Done. App bundle: dist/SimpleTranscriber.app"
echo "To distribute, wrap in a .dmg:"
echo "  brew install create-dmg"
echo "  create-dmg dist/SimpleTranscriber.app ."
