#!/usr/bin/env bash
# Runs once, when the Codespace is created.
#
# This script is deliberately forgiving. If something here fails, you should
# still get a working Codespace that you can fix from the terminal -- a setup
# hiccup must never drop the whole container into recovery mode. That is why
# there is no "set -e" and why the script always exits 0.

set -uo pipefail

echo "==> Removing stale third-party APT sources"
# The Debian base image ships an APT source for Yarn whose signing key has
# expired. Any 'apt-get update' then fails with:
#     NO_PUBKEY 62D54FD4003F6525
# This project uses no Node tooling, so the source is safe to delete.
sudo rm -f /etc/apt/sources.list.d/yarn.list \
           /etc/apt/sources.list.d/yarn.sources 2>/dev/null || true

echo "==> Installing FFmpeg (system package)"
if sudo apt-get update -y; then
  sudo apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    || echo "!!  FFmpeg install failed. Run ./scripts/setup.sh once the Codespace is up."
else
  echo "!!  apt-get update failed, so system packages were skipped."
  echo "    Run ./scripts/setup.sh once the Codespace is up."
fi

echo "==> Installing Python dependencies"
python -m pip install --upgrade pip --disable-pip-version-check || true
pip install -r requirements.txt --disable-pip-version-check \
  || echo "!!  pip install failed. Run ./scripts/setup.sh once the Codespace is up."

echo "==> Preparing local directories"
mkdir -p data/uploads data/outputs data/jobs data/fixtures data/models fonts
[ -f .env ] || cp .env.example .env
chmod +x scripts/*.sh 2>/dev/null || true

echo "==> Verifying toolchain"
ffmpeg  -hide_banner -version 2>/dev/null | head -1 || echo "    ffmpeg:  not installed yet"
ffprobe -hide_banner -version 2>/dev/null | head -1 || echo "    ffprobe: not installed yet"
python -c "import PIL, fontTools; print('    Pillow + fontTools OK')" 2>/dev/null \
  || echo "    Pillow:  not installed yet"

echo
echo "Setup finished. Start the app with:  ./scripts/dev.sh"
echo "If anything above reported a problem, run: ./scripts/setup.sh"

# Always succeed. See the note at the top of this file.
exit 0
