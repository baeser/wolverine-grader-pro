"""
License manager for Wolverine Grader Pro.

Handles device fingerprinting, license activation, offline validation,
Keychain storage, trial tracking, and update checking.

Security model: stop casual teacher sharing, not Fort Knox.
"""

import base64
import hashlib
import json
import os
import platform
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Build an SSL context that works inside PyInstaller bundles.
# The bundled Python can't find the system certificate store on macOS,
# so we use certifi's CA bundle instead.
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

# ── Paths ──────────────────────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    if sys.platform == 'darwin':
        _APP_SUPPORT = os.path.join(
            os.path.expanduser('~'), 'Library', 'Application Support', 'WolverineGraderPro'
        )
    else:
        _APP_SUPPORT = os.path.join(
            os.environ.get('APPDATA', os.path.expanduser('~')), 'WolverineGraderPro'
        )
else:
    _APP_SUPPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.wgp_data')

LICENSE_DIR = os.path.join(_APP_SUPPORT, 'license')
LICENSE_FILE = os.path.join(LICENSE_DIR, 'license.json')
TRIAL_FILE = os.path.join(LICENSE_DIR, 'trial.json')

# ── Constants ──────────────────────────────────────────────────────────────────
def _read_version():
    """Read version from VERSION file (works both frozen and dev)."""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    version_file = os.path.join(base, 'VERSION')
    try:
        with open(version_file) as f:
            return f.read().strip()
    except FileNotFoundError:
        return '3.0.0'

APP_VERSION = _read_version()
PRODUCT_ID = 'wolverine-grader-pro'
KEYCHAIN_SERVICE = 'WolverineGraderPro'
KEYCHAIN_ACCOUNT_TOKEN = 'license-token'
KEYCHAIN_ACCOUNT_KEY = 'license-key'
TRIAL_MAX_SESSIONS = 5
OFFLINE_GRACE_DAYS = 7

# License server URL (Firebase function is exported as 'api', routes are under /api/)
LICENSE_SERVER_URL = os.environ.get(
    'WGP_LICENSE_SERVER',
    'https://us-central1-wolverine-grader-pro.cloudfunctions.net/api/api'
)


# ── Device Fingerprinting ─────────────────────────────────────────────────────

def get_hardware_uuid() -> str:
    """Get the macOS hardware UUID (stable across OS reinstalls)."""
    try:
        result = subprocess.run(
            ['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if 'IOPlatformUUID' in line:
                # Extract UUID from:  "IOPlatformUUID" = "XXXXXXXX-XXXX-..."
                parts = line.split('"')
                for i, part in enumerate(parts):
                    if part == 'IOPlatformUUID' and i + 2 < len(parts):
                        return parts[i + 2]
    except Exception:
        pass
    return 'unknown-device'


def get_device_id() -> str:
    """Generate a stable, hashed device fingerprint."""
    hw_uuid = get_hardware_uuid()
    # Hash with bundle ID salt so the fingerprint is app-specific
    raw = f'{PRODUCT_ID}:{hw_uuid}'
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_device_name() -> str:
    """Get a human-readable device name."""
    try:
        result = subprocess.run(
            ['scutil', '--get', 'ComputerName'],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return platform.node() or 'Mac'


# ── macOS Keychain ─────────────────────────────────────────────────────────────

def _keychain_set(account: str, value: str) -> bool:
    """Store a value in the macOS Keychain."""
    try:
        # Delete existing entry first (ignore errors)
        subprocess.run(
            ['security', 'delete-generic-password', '-s', KEYCHAIN_SERVICE, '-a', account],
            capture_output=True, timeout=5
        )
        result = subprocess.run(
            ['security', 'add-generic-password',
             '-s', KEYCHAIN_SERVICE, '-a', account,
             '-w', value, '-U'],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _keychain_get(account: str) -> str | None:
    """Read a value from the macOS Keychain."""
    try:
        result = subprocess.run(
            ['security', 'find-generic-password',
             '-s', KEYCHAIN_SERVICE, '-a', account, '-w'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _keychain_delete(account: str) -> bool:
    """Remove a value from the macOS Keychain."""
    try:
        result = subprocess.run(
            ['security', 'delete-generic-password', '-s', KEYCHAIN_SERVICE, '-a', account],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


# ── JWT Decode (offline — no verification, just expiry check) ──────────────────

def _decode_jwt_payload(token: str) -> dict | None:
    """Decode JWT payload without signature verification.
    Used for offline expiry checks only. The server verifies properly."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        # Add base64 padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += '=' * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return None


# ── File Storage ───────────────────────────────────────────────────────────────

def _ensure_license_dir():
    os.makedirs(LICENSE_DIR, exist_ok=True)


def _save_license_file(data: dict):
    _ensure_license_dir()
    with open(LICENSE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _load_license_file() -> dict | None:
    if not os.path.exists(LICENSE_FILE):
        return None
    try:
        with open(LICENSE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _save_trial_file(data: dict):
    _ensure_license_dir()
    with open(TRIAL_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _load_trial_file() -> dict | None:
    if not os.path.exists(TRIAL_FILE):
        return None
    try:
        with open(TRIAL_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


# ── HTTP Helper ────────────────────────────────────────────────────────────────

def _api_request(endpoint: str, payload: dict, timeout: int = 15) -> dict:
    """Make an HTTPS POST to the license server."""
    url = f'{LICENSE_SERVER_URL}/{endpoint}'
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            return json.loads(body)
        except Exception:
            return {'error': f'Server error ({e.code}): {body[:200]}'}
    except urllib.error.URLError as e:
        return {'error': f'Cannot reach license server: {e.reason}'}
    except Exception as e:
        return {'error': f'Network error: {e}'}


# ── License Manager ───────────────────────────────────────────────────────────

class LicenseManager:
    """Manages license state for Wolverine Grader Pro."""

    def __init__(self):
        self._license_data = None   # Cached license metadata
        self._trial_data = None     # Cached trial metadata
        self._token = None          # Cached JWT token
        self._update_info = None    # Latest update info from server
        self._load()

    def _load(self):
        """Load license state from disk and Keychain."""
        self._license_data = _load_license_file()
        self._trial_data = _load_trial_file()
        self._token = _keychain_get(KEYCHAIN_ACCOUNT_TOKEN)

    # ── Status Queries ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get the full license status for display in the UI."""
        if self.is_licensed():
            ld = self._license_data or {}
            return {
                'status': 'licensed',
                'licensed_to': ld.get('licensed_to', ''),
                'license_key': ld.get('license_key', ''),
                'max_devices': ld.get('max_devices', 2),
                'valid_offline': self._is_token_valid_offline(),
                'last_validated': ld.get('last_validated', ''),
                'app_version': APP_VERSION,
                'update': self._update_info,
            }
        elif self.is_trial():
            td = self._trial_data or {}
            return {
                'status': 'trial',
                'sessions_used': td.get('sessions_used', 0),
                'sessions_remaining': max(0, TRIAL_MAX_SESSIONS - td.get('sessions_used', 0)),
                'max_sessions': TRIAL_MAX_SESSIONS,
                'app_version': APP_VERSION,
            }
        else:
            return {
                'status': 'unlicensed',
                'app_version': APP_VERSION,
            }

    def is_licensed(self) -> bool:
        """Is there an active license (online-verified or offline-valid)?"""
        if not self._license_data or not self._token:
            return False
        if self._license_data.get('status') != 'active':
            return False
        return self._is_token_valid_offline()

    def is_trial(self) -> bool:
        """Is this a trial with sessions remaining?"""
        if self.is_licensed():
            return False
        td = self._trial_data
        if not td:
            return False
        return td.get('sessions_used', 0) < TRIAL_MAX_SESSIONS

    def is_trial_expired(self) -> bool:
        """Has the trial been used up?"""
        td = self._trial_data
        if not td:
            return False
        return td.get('sessions_used', 0) >= TRIAL_MAX_SESSIONS

    def can_grade(self) -> bool:
        """Can the user grade essays right now?"""
        return self.is_licensed() or self.is_trial()

    def _is_token_valid_offline(self) -> bool:
        """Check if the stored JWT hasn't expired (offline grace period)."""
        if not self._token:
            return False
        payload = _decode_jwt_payload(self._token)
        if not payload:
            return False
        exp = payload.get('exp', 0)
        return time.time() < exp

    # ── Trial Management ───────────────────────────────────────────────────

    def start_trial(self) -> dict:
        """Start a free trial."""
        if self._trial_data:
            return {'success': True, 'message': 'Trial already started.', 'sessions_remaining': max(0, TRIAL_MAX_SESSIONS - self._trial_data.get('sessions_used', 0))}

        trial_data = {
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'sessions_used': 0,
            'device_id': get_device_id(),
        }
        _save_trial_file(trial_data)
        self._trial_data = trial_data
        return {
            'success': True,
            'sessions_remaining': TRIAL_MAX_SESSIONS,
            'message': f'Trial started! You have {TRIAL_MAX_SESSIONS} free grading sessions.',
        }

    def use_trial_session(self) -> bool:
        """Consume one trial session. Returns True if allowed."""
        if self.is_licensed():
            return True  # Licensed users don't use trial sessions
        if not self._trial_data:
            return False
        used = self._trial_data.get('sessions_used', 0)
        if used >= TRIAL_MAX_SESSIONS:
            return False
        self._trial_data['sessions_used'] = used + 1
        self._trial_data['last_used'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        _save_trial_file(self._trial_data)
        return True

    # ── License Activation ─────────────────────────────────────────────────

    def claim_purchase(self, order_number: str, buyer_name: str, buyer_email: str = '') -> dict:
        """Claim a TPT purchase and get a license key."""
        result = _api_request('claim-purchase', {
            'orderNumber': order_number,
            'buyerName': buyer_name,
            'buyerEmail': buyer_email,
            'productId': PRODUCT_ID,
        })
        return result

    def activate_device(self, license_key: str) -> dict:
        """Activate this device with a license key."""
        device_id = get_device_id()
        device_name = get_device_name()

        result = _api_request('activate', {
            'licenseKey': license_key,
            'deviceId': device_id,
            'deviceName': device_name,
        })

        if result.get('success'):
            token = result.get('token', '')
            licensed_to = result.get('licensedTo', '')

            # Store token in Keychain
            _keychain_set(KEYCHAIN_ACCOUNT_TOKEN, token)
            _keychain_set(KEYCHAIN_ACCOUNT_KEY, license_key)
            self._token = token

            # Save license metadata to disk
            license_data = {
                'license_key': license_key,
                'licensed_to': licensed_to,
                'device_id': device_id,
                'max_devices': result.get('maxDevices', 2),
                'status': 'active',
                'activated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'last_validated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }
            _save_license_file(license_data)
            self._license_data = license_data

            # Store update info if present
            if result.get('latestVersion'):
                self._update_info = {
                    'version': result['latestVersion'],
                    'download_url': result.get('downloadUrl', ''),
                    'release_notes': result.get('releaseNotes', ''),
                }

        return result

    def validate_online(self) -> dict:
        """Validate the license with the server and refresh the token."""
        if not self._token:
            return {'error': 'No token to validate.'}

        device_id = get_device_id()
        result = _api_request('validate', {
            'token': self._token,
            'deviceId': device_id,
            'appVersion': APP_VERSION,
        })

        if result.get('valid'):
            new_token = result.get('token', self._token)
            _keychain_set(KEYCHAIN_ACCOUNT_TOKEN, new_token)
            self._token = new_token

            # Update last validated timestamp
            if self._license_data:
                self._license_data['last_validated'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                _save_license_file(self._license_data)

            # Check for updates
            if result.get('latestVersion'):
                self._update_info = {
                    'version': result['latestVersion'],
                    'min_required': result.get('minRequiredVersion', '1.0.0'),
                    'download_url': result.get('downloadUrl', ''),
                    'release_notes': result.get('releaseNotes', ''),
                    'mandatory': result.get('mandatory', False),
                }

        elif result.get('revoked'):
            # License was revoked server-side
            self._clear_license()

        return result

    def deactivate_device(self) -> dict:
        """Deactivate this device."""
        license_key = _keychain_get(KEYCHAIN_ACCOUNT_KEY) or ''
        if not license_key and self._license_data:
            license_key = self._license_data.get('license_key', '')

        device_id = get_device_id()
        result = _api_request('deactivate', {
            'licenseKey': license_key,
            'deviceId': device_id,
        })

        if result.get('success'):
            self._clear_license()

        return result

    def _clear_license(self):
        """Remove all local license data."""
        _keychain_delete(KEYCHAIN_ACCOUNT_TOKEN)
        _keychain_delete(KEYCHAIN_ACCOUNT_KEY)
        self._token = None
        self._license_data = None
        if os.path.exists(LICENSE_FILE):
            try:
                os.remove(LICENSE_FILE)
            except OSError:
                pass

    # ── Update Info ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_version(v: str):
        """Parse '3.1.0' into a tuple (3, 1, 0) for proper comparison."""
        try:
            return tuple(int(x) for x in v.split('.'))
        except (ValueError, AttributeError):
            return (0,)

    def get_update_info(self) -> dict | None:
        """Return update info only if a newer version is available AND a download URL is configured."""
        if not self._update_info:
            return None
        latest = self._update_info.get('version', '')
        download_url = self._update_info.get('download_url', '')
        if latest and download_url and self._parse_version(latest) > self._parse_version(APP_VERSION):
            return self._update_info
        return None

    def check_for_updates(self) -> dict | None:
        """Proactively check for updates (triggers online validation)."""
        if self.is_licensed():
            self.validate_online()
            return self.get_update_info()
        return None


# ── Singleton ──────────────────────────────────────────────────────────────────
_instance = None

def get_license_manager() -> LicenseManager:
    """Get or create the singleton LicenseManager."""
    global _instance
    if _instance is None:
        _instance = LicenseManager()
    return _instance
