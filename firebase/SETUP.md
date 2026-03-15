# Firebase License Server Setup

## 1. Create Firebase Project

```bash
# Install Firebase CLI
npm install -g firebase-tools

# Log in
firebase login

# Create project (or use existing)
firebase projects:create wolverine-grader-pro
```

## 2. Enable Firestore

Go to [Firebase Console](https://console.firebase.google.com) > your project > Firestore Database > Create Database > Start in production mode.

## 3. Set Secrets

```bash
# Set the JWT signing secret (generate a strong random string)
firebase functions:config:set license.jwt_secret="YOUR_RANDOM_SECRET_HERE"

# Set the admin API key (for managing licenses)
firebase functions:config:set license.admin_key="YOUR_ADMIN_KEY_HERE"
```

## 4. Deploy

```bash
cd firebase
npm --prefix functions install
firebase deploy
```

After deployment, your API URL will be:
```
https://us-central1-wolverine-grader-pro.cloudfunctions.net/api
```

## 5. Update the App

Edit `grader/license_manager.py` and update `LICENSE_SERVER_URL` with your actual Firebase Functions URL.

## 6. Publish an App Release (for update notifications)

```bash
curl -X POST https://YOUR_URL/api/admin/release \
  -H "Content-Type: application/json" \
  -H "x-admin-key: YOUR_ADMIN_KEY" \
  -d '{
    "version": "3.1.0",
    "downloadUrl": "https://your-site.com/WolverineGraderPro-3.1.dmg",
    "releaseNotes": "New features and bug fixes",
    "minRequiredVersion": "3.0.0",
    "mandatory": false
  }'
```

## Admin API Examples

### List all licenses
```bash
curl -X POST https://YOUR_URL/api/admin/licenses \
  -H "Content-Type: application/json" \
  -H "x-admin-key: YOUR_ADMIN_KEY" \
  -d '{"search": "jane"}'
```

### Reset a license (clear all device activations)
```bash
curl -X POST https://YOUR_URL/api/admin/reset \
  -H "Content-Type: application/json" \
  -H "x-admin-key: YOUR_ADMIN_KEY" \
  -d '{"licenseId": "LIC_abc123"}'
```

### Revoke a license
```bash
curl -X POST https://YOUR_URL/api/admin/revoke \
  -H "Content-Type: application/json" \
  -H "x-admin-key: YOUR_ADMIN_KEY" \
  -d '{"licenseId": "LIC_abc123", "reason": "Shared license detected"}'
```
