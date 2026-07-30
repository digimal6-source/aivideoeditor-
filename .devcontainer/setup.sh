#!/usr/bin/env bash
# Runs once when the Codespace is created.
set -euo pipefail

echo "==> Installing FFmpeg (system package)"
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core

echo "==> Installing Python dependencies"
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "==> Preparing local directories"
mkdir -p data/uploads data/outputs data/jobs data/models fonts
[ -f .env ] || cp .env.example .env

echo "==> Verifying toolchain"
ffmpeg -version | head -1
ffprobe -version | head -1
python -c "import PIL, fontTools; print('Pillow + fontTools OK')"

echo
echo "Setup complete. Start the app with:  ./scripts/dev.sh"
