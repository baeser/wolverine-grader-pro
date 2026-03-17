#!/bin/bash
# ─────────────────────────────────────────────────────────────
# release.sh — Full build, sign, notarize, and release pipeline
#
# Usage:
#   ./release.sh              # Build & release current VERSION
#   ./release.sh 3.1.0        # Bump to 3.1.0, then build & release
#   ./release.sh --bump patch # Auto-bump patch (3.0.0 → 3.0.1)
#   ./release.sh --bump minor # Auto-bump minor (3.0.0 → 3.1.0)
#   ./release.sh --bump major # Auto-bump major (3.0.0 → 4.0.0)
#
# What it does:
#   1. Updates VERSION file and all version references
#   2. Builds .app with PyInstaller
#   3. Code-signs with Developer ID
#   4. Notarizes with Apple
#   5. Creates .dmg
#   6. Creates GitHub Release with the .dmg attached
#   7. Registers the release with Firebase (so existing users see the update)
# ─────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"

# ── Configuration ────────────────────────────────────────────
SIGNING_IDENTITY="Developer ID Application: Ryan Baese (3AJFWBPAX7)"
NOTARIZE_PROFILE="WGP-Profile"
BUNDLE_ID="com.treetownai.wolverinegraderpro"
FIREBASE_API="https://us-central1-wolverine-grader-pro.cloudfunctions.net/api/api"
FIREBASE_ADMIN_KEY="E36ZoHvEf-jMm0ub96HDtPdXhJ5uQ0AyZSQZ5K586mo"
GITHUB_REPO="baeser/wolverine-grader-pro"

APP_NAME="Wolverine Grader Pro"
APP_PATH="dist/${APP_NAME}.app"

# ── Read current version ─────────────────────────────────────
CURRENT_VERSION=$(cat VERSION | tr -d '[:space:]')
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║     Wolverine Grader Pro — Release Pipeline         ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Current version: ${CURRENT_VERSION}"

# ── Handle version argument ──────────────────────────────────
NEW_VERSION="$CURRENT_VERSION"

if [ $# -gt 0 ]; then
    if [ "$1" == "--bump" ] && [ $# -gt 1 ]; then
        IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"
        case "$2" in
            patch) PATCH=$((PATCH + 1)) ;;
            minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
            major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
            *) echo "❌ Unknown bump type: $2 (use patch, minor, or major)"; exit 1 ;;
        esac
        NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
    else
        NEW_VERSION="$1"
    fi
fi

# Derive short version (e.g., 3.1 from 3.1.0)
IFS='.' read -r V_MAJOR V_MINOR V_PATCH <<< "$NEW_VERSION"
SHORT_VERSION="${V_MAJOR}.${V_MINOR}"

echo "  Release version: ${NEW_VERSION}"
echo ""

# ── Confirm ──────────────────────────────────────────────────
read -p "  Proceed with build and release v${NEW_VERSION}? [y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "  Cancelled."
    exit 0
fi
echo ""

# ── Step 1: Update version everywhere ────────────────────────
echo "━━━ Step 1/7: Updating version to ${NEW_VERSION} ━━━"

# VERSION file (source of truth)
echo "$NEW_VERSION" > VERSION

# PyInstaller spec — CFBundleVersion and CFBundleShortVersionString
sed -i '' "s/'CFBundleVersion': '[^']*'/'CFBundleVersion': '${NEW_VERSION}'/" WolverineGraderPro.spec
sed -i '' "s/'CFBundleShortVersionString': '[^']*'/'CFBundleShortVersionString': '${SHORT_VERSION}'/" WolverineGraderPro.spec

echo "  ✅ Version updated to ${NEW_VERSION}"

# ── Step 2: Build with PyInstaller ───────────────────────────
echo ""
echo "━━━ Step 2/7: Building app with PyInstaller ━━━"

# Clean previous build
rm -rf build/ dist/

python3 -m PyInstaller WolverineGraderPro.spec --noconfirm

if [ ! -d "$APP_PATH" ]; then
    echo "  ❌ Build failed — $APP_PATH not found"
    exit 1
fi
echo "  ✅ App built: $APP_PATH"

# ── Step 3: Code sign ────────────────────────────────────────
echo ""
echo "━━━ Step 3/7: Code signing ━━━"

codesign --deep --force --options runtime \
    --sign "$SIGNING_IDENTITY" \
    "$APP_PATH"

codesign --verify --deep --strict "$APP_PATH"
echo "  ✅ Code signature valid"

# ── Step 4: Notarize ─────────────────────────────────────────
echo ""
echo "━━━ Step 4/7: Notarizing with Apple ━━━"

# Create a zip for notarization submission
NOTARIZE_ZIP="dist/WolverineGraderPro-notarize.zip"
ditto -c -k --keepParent "$APP_PATH" "$NOTARIZE_ZIP"

echo "  Submitting to Apple notary service..."
xcrun notarytool submit "$NOTARIZE_ZIP" \
    --keychain-profile "$NOTARIZE_PROFILE" \
    --wait

echo "  Stapling notarization ticket..."
xcrun stapler staple "$APP_PATH"

# Clean up notarization zip
rm -f "$NOTARIZE_ZIP"

echo "  ✅ Notarization complete"

# ── Step 5: Create DMG ───────────────────────────────────────
echo ""
echo "━━━ Step 5/7: Creating DMG ━━━"

DMG_NAME="WolverineGraderPro-${NEW_VERSION}"
DMG_FINAL="dist/${DMG_NAME}.dmg"
VOL_NAME="Wolverine Grader Pro ${NEW_VERSION}"
DMG_STAGING="dist/dmg_staging"

rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"

# Copy app (preserving signatures and extended attributes)
cp -a "$APP_PATH" "$DMG_STAGING/"

# Add Applications symlink for drag-and-drop install
ln -s /Applications "$DMG_STAGING/Applications"

# Build the DMG
hdiutil create \
    -volname "$VOL_NAME" \
    -srcfolder "$DMG_STAGING" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_FINAL"

rm -rf "$DMG_STAGING"

DMG_SIZE=$(du -h "$DMG_FINAL" | cut -f1)
echo "  ✅ DMG created: $DMG_FINAL ($DMG_SIZE)"

# ── Step 6: GitHub Release ───────────────────────────────────
echo ""
echo "━━━ Step 6/7: Creating GitHub Release ━━━"

# Prompt for release notes
echo "  Enter release notes (press Enter for default):"
read -p "  > " RELEASE_NOTES
if [ -z "$RELEASE_NOTES" ]; then
    RELEASE_NOTES="Wolverine Grader Pro v${NEW_VERSION}"
fi

TAG="v${NEW_VERSION}"

# Create the release and upload the DMG
gh release create "$TAG" "$DMG_FINAL" \
    --repo "$GITHUB_REPO" \
    --title "Wolverine Grader Pro ${NEW_VERSION}" \
    --notes "$(cat <<EOF
## Wolverine Grader Pro ${NEW_VERSION}

${RELEASE_NOTES}

### Installation
1. Download \`${DMG_NAME}.dmg\` below
2. Open the DMG and drag **Wolverine Grader Pro** to your Applications folder
3. Launch from Applications

*Signed and notarized for macOS — no security warnings.*
EOF
)"

# Get the download URL for the DMG asset
DOWNLOAD_URL=$(gh release view "$TAG" --repo "$GITHUB_REPO" --json assets --jq ".assets[] | select(.name == \"${DMG_NAME}.dmg\") | .url")

echo "  ✅ GitHub Release created: $TAG"
echo "  📦 Download URL: $DOWNLOAD_URL"

# ── Step 7: Register with Firebase ───────────────────────────
echo ""
echo "━━━ Step 7/7: Registering release with Firebase ━━━"

curl -s -X POST "${FIREBASE_API}/admin/release" \
    -H "Content-Type: application/json" \
    -H "x-admin-key: ${FIREBASE_ADMIN_KEY}" \
    -d "$(cat <<EOF
{
    "version": "${NEW_VERSION}",
    "downloadUrl": "${DOWNLOAD_URL}",
    "releaseNotes": "${RELEASE_NOTES}",
    "minRequiredVersion": "3.0.0",
    "mandatory": false
}
EOF
)" | python3 -m json.tool

echo ""
echo "  ✅ Release registered with Firebase"

# ── Done ─────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ✅  Release v${NEW_VERSION} complete!                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  DMG:     $DMG_FINAL"
echo "  GitHub:  https://github.com/${GITHUB_REPO}/releases/tag/${TAG}"
echo "  Size:    $DMG_SIZE"
echo ""
echo "  Existing users will see an update banner next time they open the app."
echo ""
