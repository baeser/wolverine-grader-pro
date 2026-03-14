#!/bin/bash
# ─────────────────────────────────────────────────────────────
# create_dmg.sh — Build a distributable .dmg for Wolverine Grader Pro
#
# This creates a .dmg disk image with the signed/notarized .app inside.
# DMGs preserve code signatures and extended attributes, unlike .zip files.
#
# Usage:  ./create_dmg.sh
# ─────────────────────────────────────────────────────────────
set -euo pipefail

APP_NAME="Wolverine Grader Pro"
APP_PATH="dist/${APP_NAME}.app"
DMG_NAME="WolverineGraderPro-3.0"
DMG_TEMP="dist/${DMG_NAME}-temp.dmg"
DMG_FINAL="dist/${DMG_NAME}.dmg"
VOL_NAME="Wolverine Grader Pro 3.0"
DMG_SIZE="500m"

# Change to script directory
cd "$(dirname "$0")"

# ── Preflight checks ──────────────────────────────────────────
if [ ! -d "$APP_PATH" ]; then
    echo "❌ App not found at $APP_PATH"
    echo "   Run PyInstaller first:  pyinstaller WolverineGraderPro.spec"
    exit 1
fi

echo "✅ Found $APP_PATH"

# Verify signing
echo "🔍 Verifying code signature..."
codesign --verify --deep --strict "$APP_PATH" 2>&1 && echo "✅ Code signature valid" || {
    echo "❌ Code signature invalid. Re-sign the app before creating DMG."
    exit 1
}

# Verify notarization staple
echo "🔍 Checking notarization staple..."
stapler validate "$APP_PATH" 2>&1 && echo "✅ Notarization ticket stapled" || {
    echo "⚠️  No notarization ticket stapled. The DMG will still work but"
    echo "   users may see a delay on first launch while macOS checks online."
    echo "   To staple: xcrun stapler staple \"$APP_PATH\""
}

# ── Clean up previous builds ──────────────────────────────────
rm -f "$DMG_TEMP" "$DMG_FINAL"

# ── Create temporary DMG ──────────────────────────────────────
echo ""
echo "📦 Creating DMG..."

# Create a temporary directory for DMG contents
DMG_STAGING="dist/dmg_staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"

# Copy the app (preserving extended attributes and signatures!)
echo "   Copying app bundle (preserving signatures)..."
cp -a "$APP_PATH" "$DMG_STAGING/"

# Add a symlink to /Applications for drag-and-drop install
ln -s /Applications "$DMG_STAGING/Applications"

# Create the DMG from the staging directory
echo "   Building disk image..."
hdiutil create \
    -volname "$VOL_NAME" \
    -srcfolder "$DMG_STAGING" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_FINAL"

# Clean up staging
rm -rf "$DMG_STAGING"

echo ""
echo "✅ DMG created: $DMG_FINAL"

# ── Show final info ───────────────────────────────────────────
DMG_SIZE_MB=$(du -h "$DMG_FINAL" | cut -f1)
echo "   Size: $DMG_SIZE_MB"
echo ""
echo "📋 Distribution checklist:"
echo "   1. Upload the .dmg to your website or file sharing service"
echo "   2. Teachers download and double-click the .dmg"
echo "   3. They drag 'Wolverine Grader Pro' to the Applications folder"
echo "   4. They launch from Applications — no security warnings!"
echo ""
echo "   ⚠️  Do NOT zip the .dmg — share the .dmg file directly."
