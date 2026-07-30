#!/usr/bin/env bash
# One-time setup for Clipforge.
#
# Safe to run more than once. It never fails the whole script just because an
# optional extra (faster-whisper) could not be installed.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mnote\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$*"; }

say "1/5  Checking FFmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
  ok "$(ffmpeg -hide_banner -version | head -n 1)"
else
  warn "FFmpeg is not installed. Trying to install it..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y ffmpeg
  elif command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  fi
  if command -v ffmpeg >/dev/null 2>&1; then
    ok "FFmpeg installed"
  else
    bad "FFmpeg could not be installed automatically. See TROUBLESHOOTING.md."
    exit 1
  fi
fi

say "2/5  Checking Python"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  bad "python3 was not found on PATH."
  exit 1
fi
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Python 3.10+ is required, found {sys.version.split()[0]}")
print(f"  ok    Python {sys.version.split()[0]}")
EOF

say "3/5  Installing Python packages"
# The app itself runs on the standard library plus Pillow, so this step is
# small and fast. Everything optional lives in requirements-optional.txt.
if "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt; then
  ok "required packages installed"
else
  warn "pip install failed. Checking whether the app can still run..."
  if "$PY" -c "import PIL" >/dev/null 2>&1; then
    ok "Pillow is already available; continuing"
  else
    bad "Pillow is required for hook text rendering and could not be installed."
    exit 1
  fi
fi

say "4/5  Optional: local speech-to-text (faster-whisper)"
if "$PY" -c "import faster_whisper" >/dev/null 2>&1; then
  ok "faster-whisper already installed"
else
  if [ "${SKIP_WHISPER:-0}" = "1" ]; then
    warn "skipped (SKIP_WHISPER=1)"
  elif "$PY" -m pip install --quiet --disable-pip-version-check -r requirements-optional.txt; then
    ok "faster-whisper installed"
  else
    warn "faster-whisper could not be installed."
    warn "The app still works: choose 'Paste transcript manually' as the caption source."
  fi
fi

say "5/5  Preparing directories and configuration"
mkdir -p data/uploads data/outputs data/jobs data/fixtures data/models fonts
ok "data/ and fonts/ ready"

if [ ! -f .env ]; then
  cp .env.example .env
  ok ".env created from .env.example"
else
  ok ".env already exists (left untouched)"
fi

chmod +x scripts/*.sh 2>/dev/null || true

say "Setup complete"
cat <<EOF
  Start the app:      ./scripts/dev.sh
  Run the tests:      $PY -m unittest discover -s tests -t .
  Run the smoke test: $PY scripts/smoke_test.py

  Fonts: drop Indivisible and Rubik Bold (.ttf/.otf) into $REPO_ROOT/fonts,
  or upload them from the web interface. See fonts/README.md.
EOF
