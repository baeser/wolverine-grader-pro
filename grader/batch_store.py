"""
Persist batch job metadata to disk.
Saves to <app_root>/batch_jobs/<session_id>.json
"""
import hashlib
import json
import os

from .session_store import _app_root, _safe


BATCH_DIR = os.path.join(_app_root, 'batch_jobs')


def _ensure_dir() -> None:
    os.makedirs(BATCH_DIR, exist_ok=True)


def hash_key(api_key: str) -> str:
    """SHA-256 hash of the API key for matching (NOT stored raw)."""
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:16]


def save_batch(session_id: str, batch_data: dict) -> None:
    """Save batch metadata to disk."""
    if not _safe(session_id):
        raise ValueError("Invalid session ID")
    _ensure_dir()
    path = os.path.join(BATCH_DIR, f"{session_id}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(batch_data, f, indent=2, default=str)


def load_batch(session_id: str) -> dict | None:
    """Load batch metadata from disk."""
    if not _safe(session_id):
        return None
    path = os.path.join(BATCH_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def update_batch_status(session_id: str, status: str,
                        completed_at: str = None) -> None:
    """Update just the status field of a saved batch."""
    batch = load_batch(session_id)
    if not batch:
        return
    batch['status'] = status
    if completed_at:
        batch['completed_at'] = completed_at
    save_batch(session_id, batch)


def list_pending_batches() -> list:
    """Return all batch jobs with status 'pending' or 'in_progress'."""
    _ensure_dir()
    pending = []
    try:
        for fname in os.listdir(BATCH_DIR):
            if not fname.endswith('.json'):
                continue
            path = os.path.join(BATCH_DIR, fname)
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('status') in ('pending', 'in_progress'):
                    pending.append(data)
            except Exception:
                continue
    except OSError:
        pass
    return pending


def delete_batch(session_id: str) -> bool:
    """Remove batch metadata file."""
    if not _safe(session_id):
        return False
    path = os.path.join(BATCH_DIR, f"{session_id}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False
