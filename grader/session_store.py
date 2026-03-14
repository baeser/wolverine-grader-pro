"""
Disk-based persistence for grading sessions.
Saves to  <app_root>/saved_sessions/<session_id>.json
Canvas tokens are intentionally NOT persisted for security.
"""
import json
import os
import re

import sys

# Store sessions in a user-specific location so they are NEVER bundled inside the .app
if getattr(sys, 'frozen', False):
    # macOS: ~/Library/Application Support/WolverineGraderPro/saved_sessions
    # Windows: %APPDATA%/WolverineGraderPro/saved_sessions
    if sys.platform == 'darwin':
        _app_root = os.path.join(
            os.path.expanduser('~'), 'Library', 'Application Support', 'WolverineGraderPro'
        )
    else:
        _app_root = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'WolverineGraderPro')
else:
    _app_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')

SESSIONS_DIR = os.path.join(_app_root, 'saved_sessions')

# Only allow standard UUID-shaped IDs to prevent path traversal
_UUID_RE = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')


def _safe(session_id: str) -> bool:
    return bool(_UUID_RE.match(session_id))


def _ensure_dir() -> None:
    os.makedirs(SESSIONS_DIR, exist_ok=True)


MAX_SAVED_SESSIONS = 5


def save_session(session_id: str, data: dict) -> None:
    if not _safe(session_id):
        raise ValueError("Invalid session ID")
    _ensure_dir()
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    # Prune oldest sessions beyond the cap (keep the most recent)
    _prune_old_sessions(session_id)


def _prune_old_sessions(keep_id: str) -> None:
    """Delete the oldest saved sessions if we exceed MAX_SAVED_SESSIONS."""
    try:
        files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.json')]
    except OSError:
        return
    if len(files) <= MAX_SAVED_SESSIONS:
        return
    # Sort by modification time, oldest first
    files_with_mtime = []
    for f in files:
        p = os.path.join(SESSIONS_DIR, f)
        try:
            files_with_mtime.append((os.path.getmtime(p), f))
        except OSError:
            continue
    files_with_mtime.sort()
    # Delete oldest until we're at the cap
    to_delete = len(files_with_mtime) - MAX_SAVED_SESSIONS
    for _, f in files_with_mtime[:to_delete]:
        sid = f[:-5]  # strip .json
        if sid == keep_id:
            continue  # never delete the one we just saved
        try:
            os.remove(os.path.join(SESSIONS_DIR, f))
        except OSError:
            pass


def load_session(session_id: str) -> dict | None:
    if not _safe(session_id):
        return None
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def list_sessions() -> list:
    _ensure_dir()
    sessions = []
    try:
        filenames = sorted(os.listdir(SESSIONS_DIR), reverse=True)
    except OSError:
        return []
    for fname in filenames:
        if not fname.endswith('.json'):
            continue
        path = os.path.join(SESSIONS_DIR, fname)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            sessions.append({
                'session_id':      data.get('session_id', fname[:-5]),
                'saved_at':        data.get('saved_at', ''),
                'label':           data.get('label', 'Session'),
                'total':           data.get('total', 0),
                'model_label':     data.get('model_label', ''),
                'strictness_emoji': data.get('strictness_emoji', ''),
                'canvas_enabled':  data.get('canvas_enabled', False),
            })
        except Exception:
            continue
    return sessions


def delete_session(session_id: str) -> bool:
    if not _safe(session_id):
        return False
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False
