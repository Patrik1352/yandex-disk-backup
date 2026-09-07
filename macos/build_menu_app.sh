#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DESTINATION="${1:-$SCRIPT_DIR/dist/Yandex Backup.app}"
if [[ "$DESTINATION" != *.app ]]; then
    echo "Usage: $0 [destination.app]" >&2
    exit 2
fi
if [[ "$(uname -s)" != Darwin ]]; then
    echo "The menu app requires macOS and Apple's Command Line Tools." >&2
    exit 2
fi

# Build in a sibling temporary directory before replacing this app's files.
# A failed compiler run therefore leaves an existing installation untouched.
mkdir -p "$(dirname "$DESTINATION")"
BUILD_DIR="$(mktemp -d "$(dirname "$DESTINATION")/.yandex-menu-build.XXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT
APP_DIR="$BUILD_DIR/Yandex Backup.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources" "$BUILD_DIR/module-cache"
xcrun swiftc -O -swift-version 5 -parse-as-library -framework AppKit \
    -target "$(uname -m)-apple-macosx13.0" \
    -module-cache-path "$BUILD_DIR/module-cache" \
    "$SCRIPT_DIR/MenuBar.swift" -o "$APP_DIR/Contents/MacOS/YandexBackup"
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>local.egor.yandexbackup</string>
  <key>CFBundleName</key><string>Яндекс Бэкап</string>
  <key>CFBundleDisplayName</key><string>Яндекс Бэкап</string>
  <key>CFBundleExecutable</key><string>YandexBackup</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.3.1</string>
  <key>CFBundleVersion</key><string>4</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSDesktopFolderUsageDescription</key><string>Резервное копирование выбранных файлов рабочего стола на ваш Яндекс.Диск</string>
</dict></plist>
PLIST
/usr/bin/plutil -lint "$APP_DIR/Contents/Info.plist"
/usr/bin/codesign --force --sign - "$APP_DIR"
/usr/bin/ditto "$APP_DIR" "$DESTINATION"
echo "Built: $DESTINATION"
