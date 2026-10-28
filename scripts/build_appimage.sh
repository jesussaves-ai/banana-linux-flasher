#!/usr/bin/env bash
#
# Build a Linux AppImage of the BananaLinux Flasher.
#
# Usage:
#   scripts/build_appimage.sh [VERSION] [OUTPUT_DIR]
#
# Requirements:
#   * python3 with Tkinter and PyInstaller installed (or PYTHON=... to point at
#     an interpreter that has them)
#   * curl or wget, and network access to fetch appimagetool on first run
#
# Produces: <OUTPUT_DIR>/BananaLinuxFlasher-<VERSION>-<arch>.AppImage
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' "$ROOT/flash_banana.py")}"
OUTDIR="${2:-$ROOT/dist}"
PYTHON="${PYTHON:-python3}"

ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in
  x86_64 | amd64) APPIMAGE_ARCH=x86_64 ;;
  aarch64 | arm64) APPIMAGE_ARCH=aarch64 ;;
  *) APPIMAGE_ARCH="$ARCH" ;;
esac

if [ -z "$VERSION" ]; then
  echo "error: could not determine version" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Building Linux binary with PyInstaller (version $VERSION, $APPIMAGE_ARCH)"
(
  cd "$ROOT"
  "$PYTHON" -m PyInstaller --clean --noconfirm BananaLinuxFlasher.spec
)
BIN="$ROOT/dist/BananaLinuxFlasher"
[ -x "$BIN" ] || { echo "error: PyInstaller did not produce $BIN" >&2; exit 1; }

echo "==> Assembling AppDir"
APPDIR="$WORK/BananaLinuxFlasher.AppDir"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"
install -m 0755 "$BIN" "$APPDIR/usr/bin/BananaLinuxFlasher"
install -m 0644 "$ROOT/docs/banana.png" "$APPDIR/bananalinux-flasher.png"
install -m 0644 "$ROOT/docs/banana.png" \
  "$APPDIR/usr/share/icons/hicolor/256x256/apps/bananalinux-flasher.png"

cat > "$APPDIR/bananalinux-flasher.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=BananaLinux Flasher
Comment=Flash BananaLinux to a microSD card for Anbernic RG DS handhelds
Exec=BananaLinuxFlasher
Icon=bananalinux-flasher
Categories=Utility;System;
Terminal=false
EOF
install -m 0644 "$APPDIR/bananalinux-flasher.desktop" \
  "$APPDIR/usr/share/applications/bananalinux-flasher.desktop"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# AppRun: launch the bundled BananaLinux Flasher.
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/BananaLinuxFlasher" "$@"
EOF
chmod +x "$APPDIR/AppRun"

echo "==> Fetching appimagetool"
APPIMAGETOOL="${APPIMAGETOOL:-$WORK/appimagetool-$APPIMAGE_ARCH.AppImage}"
if [ ! -x "$APPIMAGETOOL" ]; then
  URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$APPIMAGE_ARCH.AppImage"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 -o "$APPIMAGETOOL" "$URL"
  else
    wget -O "$APPIMAGETOOL" "$URL"
  fi
  chmod +x "$APPIMAGETOOL"
fi

echo "==> Building AppImage"
mkdir -p "$OUTDIR"
OUT="$OUTDIR/BananaLinuxFlasher-$VERSION-$APPIMAGE_ARCH.AppImage"
# --appimage-extract-and-run avoids needing FUSE (works in containers/CI too).
ARCH="$APPIMAGE_ARCH" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
echo "==> Wrote $OUT"
