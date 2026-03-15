import json
import os
import queue
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime
from io import BytesIO

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)

from config import (MAX_ZIP_SIZE_MB, SUPPORTED_ESSAY_EXTENSIONS,
                    SUPPORTED_RUBRIC_EXTENSIONS)
from grader.ai_client import GraderAI, GradingError, ALLOWED_MODELS, DEFAULT_MODELS, MODEL_LABELS
from grader.canvas_client import CanvasClient, CanvasError
from grader.extractor import ExtractionError, extract_text
from grader.prompt_builder import get_strictness_info, DEFAULT_STRICTNESS, build_rubric_generation_prompt
from grader import session_store

# When running as a PyInstaller bundle, resolve bundled data files correctly
if getattr(sys, 'frozen', False):
    _base = sys._MEIPASS
else:
    _base = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__,
            template_folder=os.path.join(_base, 'templates'),
            static_folder=os.path.join(_base, 'static'))
app.secret_key = os.urandom(32)

# In-memory storage keyed by session_id
sessions = {}


def _get_session(session_id):
    """Return the in-memory session, restoring from disk if needed."""
    if session_id in sessions:
        return sessions[session_id]
    # Try to restore from saved session on disk
    save_data = session_store.load_session(session_id)
    if not save_data:
        return None
    # Apply teacher score edits (same logic as api_session_load)
    results = []
    for r in save_data.get('results', []):
        mr = dict(r)
        if 'edited_score' in r:
            mr['score'] = r['edited_score']
        results.append(mr)
    sessions[session_id] = {
        'status':           'complete',
        'essays':           save_data.get('essays', []),
        'rubric':           save_data.get('rubric', ''),
        'strictness':       save_data.get('strictness', DEFAULT_STRICTNESS),
        'strictness_label': save_data.get('strictness_label', ''),
        'strictness_emoji': save_data.get('strictness_emoji', ''),
        'model':            save_data.get('model', ''),
        'model_label':      save_data.get('model_label', ''),
        'results':          results,
        'progress':         queue.Queue(),
        'total':            save_data.get('total', len(results)),
        'current':          len(results),
        'canvas_enabled':   save_data.get('canvas_enabled', False),
        'canvas_url':       save_data.get('canvas_url', ''),
        'canvas_token':     '',
        'canvas_course_id':     None,
        'canvas_assignment_id': None,
        'canvas_points_possible': None,
    }
    return sessions[session_id]

# Canvas pre-fetched submission data (keyed by temp UUID, expires after 1 hour)
canvas_prefetch = {}

# Canvas background fetch tasks (keyed by fetch UUID)
canvas_fetch_tasks = {}


def _cleanup_canvas_prefetch():
    """Remove expired prefetch entries."""
    now = time.time()
    expired = [k for k, v in canvas_prefetch.items() if v.get('expires', 0) < now]
    for k in expired:
        del canvas_prefetch[k]


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/grade', methods=['POST'])
def grade():
    provider = request.form.get('provider', '').strip()
    api_key = request.form.get('api_key', '').strip()
    rubric_text = request.form.get('rubric_text', '').strip()
    rubric_file = request.files.get('rubric_file')
    zip_file = request.files.get('zip_file')
    try:
        strictness = int(request.form.get('strictness', DEFAULT_STRICTNESS))
        strictness = max(2, min(10, strictness))
    except (ValueError, TypeError):
        strictness = DEFAULT_STRICTNESS

    # Resolve model
    requested_model = request.form.get('model', '').strip()
    model = requested_model if requested_model in ALLOWED_MODELS.get(provider, {}) \
        else DEFAULT_MODELS.get(provider, '')

    essay_mode = request.form.get('essay_mode', 'zip').strip()

    # Validate inputs
    if not provider or provider not in ('claude', 'openai', 'gemini'):
        return jsonify({'error': 'Please select an AI provider.'}), 400
    if not api_key:
        return jsonify({'error': 'Please enter your API key.'}), 400
    if essay_mode not in ('zip', 'files', 'canvas'):
        return jsonify({'error': 'Invalid essay upload mode.'}), 400

    # Extract rubric
    if rubric_file and rubric_file.filename:
        ext = os.path.splitext(rubric_file.filename)[1].lower()
        if ext not in SUPPORTED_RUBRIC_EXTENSIONS:
            return jsonify({'error': f'Unsupported rubric file type: {ext}'}), 400
        try:
            rubric_text = extract_text(rubric_file.filename, rubric_file.read())
        except ExtractionError as e:
            return jsonify({'error': f'Could not read rubric: {e}'}), 400

    if not rubric_text:
        return jsonify({'error': 'Please provide a rubric (paste text or upload a file).'}), 400

    # ── Extract essays — ZIP mode ─────────────────────────────────────────────
    essays = []
    canvas_meta = {}

    if essay_mode == 'zip':
        if not zip_file or not zip_file.filename:
            return jsonify({'error': 'Please upload a ZIP file of essays.'}), 400

        zip_bytes = zip_file.read()
        if len(zip_bytes) > MAX_ZIP_SIZE_MB * 1024 * 1024:
            return jsonify({'error': f'ZIP file exceeds {MAX_ZIP_SIZE_MB}MB limit.'}), 400

        try:
            with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
                for name in sorted(zf.namelist()):
                    if '__MACOSX' in name or os.path.basename(name).startswith('.'):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in SUPPORTED_ESSAY_EXTENSIONS:
                        continue
                    try:
                        file_bytes = zf.read(name)
                        text = extract_text(name, file_bytes)
                        essays.append({'filename': os.path.basename(name), 'text': text})
                    except ExtractionError as e:
                        essays.append({
                            'filename': os.path.basename(name),
                            'text': None, 'error': str(e),
                        })
        except zipfile.BadZipFile:
            return jsonify({'error': 'The uploaded file is not a valid ZIP.'}), 400

        if not essays:
            return jsonify({'error': 'No valid essay files (.docx, .pdf, .txt) found in the ZIP.'}), 400

    # ── Extract essays — individual files mode ────────────────────────────────
    elif essay_mode == 'files':
        uploaded_files = request.files.getlist('essay_files')
        if not uploaded_files or all(not f.filename for f in uploaded_files):
            return jsonify({'error': 'Please select at least one essay file.'}), 400

        for f in sorted(uploaded_files, key=lambda x: x.filename or ''):
            if not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in SUPPORTED_ESSAY_EXTENSIONS:
                continue
            try:
                file_bytes = f.read()
                text = extract_text(f.filename, file_bytes)
                essays.append({'filename': os.path.basename(f.filename), 'text': text})
            except ExtractionError as e:
                essays.append({
                    'filename': os.path.basename(f.filename),
                    'text': None, 'error': str(e),
                })

        if not essays:
            return jsonify({'error': 'None of the uploaded files could be read. Please use .docx, .pdf, or .txt files.'}), 400

    # ── Canvas mode — use pre-fetched data ────────────────────────────────────
    else:
        prefetch_id = request.form.get('canvas_prefetch_id', '').strip()
        _cleanup_canvas_prefetch()
        prefetch = canvas_prefetch.get(prefetch_id)
        if not prefetch:
            return jsonify({'error': 'Canvas session expired. Please fetch submissions again.'}), 400

        essays = prefetch['essays']
        canvas_meta = {
            'canvas_enabled': True,
            'canvas_url': prefetch['canvas_url'],
            'canvas_token': prefetch['canvas_token'],
            'canvas_course_id': prefetch['course_id'],
            'canvas_assignment_id': prefetch['assignment_id'],
            'canvas_points_possible': prefetch.get('points_possible'),
        }

        if not essays:
            return jsonify({'error': 'No valid submissions found in Canvas. Nothing to grade.'}), 400

    # ── Create session ────────────────────────────────────────────────────────
    session_id = str(uuid.uuid4())
    session['session_id'] = session_id
    strictness_info = get_strictness_info(strictness)
    model_label = MODEL_LABELS.get(model, model)
    sess_data = {
        'status': 'grading',
        'essays': essays,
        'rubric': rubric_text,
        'strictness': strictness,
        'strictness_label': strictness_info['label'],
        'strictness_emoji': strictness_info['emoji'],
        'model': model,
        'model_label': model_label,
        'results': [],
        'progress': queue.Queue(),
        'total': len(essays),
        'current': 0,
    }
    sess_data.update(canvas_meta)
    sessions[session_id] = sess_data

    thread = threading.Thread(
        target=_grade_essays,
        args=(session_id, provider, api_key, rubric_text, essays, strictness, model),
        daemon=True,
    )
    thread.start()

    return jsonify({
        'session_id': session_id,
        'total': len(essays),
        'strictness': strictness,
        'strictness_label': strictness_info['label'],
        'strictness_emoji': strictness_info['emoji'],
        'model': model,
        'model_label': model_label,
    })


def _grade_essays(session_id, provider, api_key, rubric, essays,
                  strictness=DEFAULT_STRICTNESS, model=None):
    sess = sessions.get(session_id)
    if not sess:
        return

    try:
        grader = GraderAI(provider, api_key, model=model)
    except GradingError as e:
        sess['progress'].put({'type': 'error', 'message': str(e), 'fatal': True})
        sess['status'] = 'error'
        return

    for i, essay in enumerate(essays):
        sess['current'] = i + 1

        if essay.get('error'):
            result = {
                'filename': essay['filename'],
                'score': None, 'max_score': None,
                'summary': None, 'error': essay['error'],
            }
        else:
            try:
                graded = grader.grade_essay(rubric, essay['text'], essay['filename'], strictness)
                result = {
                    'filename': essay['filename'],
                    'score': graded['score'],
                    'max_score': graded['max_score'],
                    'summary': graded['summary'],
                    'categories': graded.get('categories', []),
                    'error': None,
                }
            except (GradingError, Exception) as e:
                result = {
                    'filename': essay['filename'],
                    'score': None, 'max_score': None,
                    'summary': None, 'error': str(e),
                }

        # Carry over Canvas user ID if present
        if essay.get('canvas_user_id'):
            result['canvas_user_id'] = essay['canvas_user_id']

        sess['results'].append(result)
        sess['progress'].put({
            'type': 'progress',
            'current': i + 1,
            'total': len(essays),
            'filename': essay['filename'],
            'status': 'error' if result['error'] else 'done',
        })

    sess['progress'].put({'type': 'complete'})
    sess['status'] = 'complete'


# ── Canvas API proxy routes ───────────────────────────────────────────────────

@app.route('/api/canvas/courses')
def canvas_courses():
    canvas_url = request.args.get('canvas_url', '').strip()
    canvas_token = request.args.get('canvas_token', '').strip()
    if not canvas_url or not canvas_token:
        return jsonify({'error': 'Canvas URL and token are required.'}), 400
    try:
        client = CanvasClient(canvas_url, canvas_token)
        courses = client.get_courses()
        return jsonify({'courses': courses})
    except CanvasError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {e}'}), 500


@app.route('/api/canvas/assignments')
def canvas_assignments():
    canvas_url = request.args.get('canvas_url', '').strip()
    canvas_token = request.args.get('canvas_token', '').strip()
    course_id = request.args.get('course_id', '').strip()
    if not all([canvas_url, canvas_token, course_id]):
        return jsonify({'error': 'canvas_url, canvas_token, and course_id are required.'}), 400
    try:
        client = CanvasClient(canvas_url, canvas_token)
        assignments = client.get_assignments(int(course_id))
        return jsonify({'assignments': assignments})
    except CanvasError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {e}'}), 500


@app.route('/api/canvas/start-fetch', methods=['POST'])
def canvas_start_fetch():
    data = request.get_json()
    canvas_url    = (data.get('canvas_url')    or '').strip()
    canvas_token  = (data.get('canvas_token')  or '').strip()
    course_id     = data.get('course_id')
    assignment_id = data.get('assignment_id')
    points_possible = data.get('points_possible')

    if not all([canvas_url, canvas_token, course_id, assignment_id]):
        return jsonify({'error': 'Missing required fields.'}), 400

    fetch_id = str(uuid.uuid4())
    canvas_fetch_tasks[fetch_id] = {
        'status': 'running',
        'queue': queue.Queue(),
    }

    thread = threading.Thread(
        target=_do_canvas_fetch,
        args=(fetch_id, canvas_url, canvas_token, course_id, assignment_id, points_possible),
        daemon=True,
    )
    thread.start()

    return jsonify({'fetch_id': fetch_id})


@app.route('/api/canvas/fetch-progress/<fetch_id>')
def canvas_fetch_progress(fetch_id):
    task = canvas_fetch_tasks.get(fetch_id)
    if not task:
        return jsonify({'error': 'Fetch task not found.'}), 404

    def stream():
        q = task['queue']
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg['type'] in ('complete', 'error'):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(stream(), mimetype='text/event-stream')


def _extract_gdoc_text(url_or_text: str):
    """Try to extract essay text from a Google Docs/Drive URL. Returns text or None."""
    import re as _re
    gdoc_id = None
    for pattern in [r'/document/d/([a-zA-Z0-9_-]+)', r'/file/d/([a-zA-Z0-9_-]+)', r'[?&]id=([a-zA-Z0-9_-]+)']:
        m = _re.search(pattern, url_or_text)
        if m:
            gdoc_id = m.group(1)
            break
    if not gdoc_id:
        return None

    # Try plain text export
    export_url = f'https://docs.google.com/document/d/{gdoc_id}/export?format=txt'
    try:
        req = urllib.request.Request(export_url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        with urllib.request.urlopen(req, timeout=30) as resp:
            # Check that Google actually returned text, not an HTML sign-in page
            content_type = resp.headers.get('Content-Type', '')
            if 'text/html' in content_type:
                pass  # Google returned a login page, not the doc
            else:
                text = resp.read().decode('utf-8', errors='replace').strip()
                if text and len(text) > 50 and '<html' not in text.lower()[:200]:
                    return text
    except Exception:
        pass

    # Fallback: try PDF export
    pdf_url = f'https://docs.google.com/document/d/{gdoc_id}/export?format=pdf'
    try:
        req = urllib.request.Request(pdf_url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get('Content-Type', '')
            if 'pdf' in content_type:
                pdf_bytes = resp.read()
                return extract_text('export.pdf', pdf_bytes)
    except Exception:
        pass

    return None


def _do_canvas_fetch(fetch_id, canvas_url, canvas_token, course_id, assignment_id, points_possible):
    import re as _re
    task = canvas_fetch_tasks.get(fetch_id)
    if not task:
        return
    q = task['queue']

    try:
        client = CanvasClient(canvas_url, canvas_token)
        submissions = client.get_submissions(int(course_id), int(assignment_id))
    except CanvasError as e:
        q.put({'type': 'error', 'message': str(e)})
        task['status'] = 'error'
        return
    except Exception as e:
        q.put({'type': 'error', 'message': f'Error fetching submissions: {e}'})
        task['status'] = 'error'
        return

    total = len(submissions)
    q.put({'type': 'start', 'total': total})

    essays = []
    preview = []
    skipped = 0

    for i, sub in enumerate(submissions):
        user = sub.get('user') or {}
        student_name = user.get('name') or user.get('login_id') or f"Student {sub.get('user_id', '?')}"
        user_id = sub.get('user_id')
        sub_type = sub.get('submission_type')

        if not sub_type:
            skipped += 1
            q.put({'type': 'progress', 'current': i + 1, 'total': total,
                   'name': student_name, 'status': 'skipped', 'reason': 'No submission yet'})
            continue

        essay_text = None
        source_filename = None

        if sub_type == 'online_upload':
            attachments = sub.get('attachments') or []
            for att in attachments:
                fname = att.get('filename') or att.get('display_name') or 'file'
                ext = os.path.splitext(fname)[1].lower()
                if ext not in SUPPORTED_ESSAY_EXTENSIONS:
                    continue
                try:
                    file_bytes = client.download_file(att['url'])
                    essay_text = extract_text(fname, file_bytes)
                    source_filename = fname
                    break
                except (CanvasError, ExtractionError):
                    continue

        elif sub_type == 'online_text_entry':
            body = sub.get('body') or ''
            if body.strip():
                plain = _re.sub(r'<[^>]+>', ' ', body).strip()
                # Check if the "text entry" is actually just a Google Drive link
                gdoc_from_body = _extract_gdoc_text(plain)
                if gdoc_from_body:
                    essay_text = gdoc_from_body
                    source_filename = 'google_doc'
                elif not _re.match(r'^https?://\S+$', plain):
                    # Only use as essay if it's actual text, not a bare URL
                    essay_text = plain
                    source_filename = 'text_entry'
                else:
                    skipped += 1
                    reason = 'Submission is a URL link, not essay text'
                    preview.append({'name': student_name, 'status': 'skipped', 'reason': reason})
                    q.put({'type': 'progress', 'current': i + 1, 'total': total,
                           'name': student_name, 'status': 'skipped', 'reason': reason})
                    continue

        elif sub_type == 'online_url':
            submitted_url = sub.get('url') or ''
            gdoc_text = _extract_gdoc_text(submitted_url)
            if gdoc_text:
                essay_text = gdoc_text
                source_filename = 'google_doc'
            else:
                skipped += 1
                reason = 'Google Drive link not publicly accessible' if 'google.com' in submitted_url else f'URL submission not supported'
                preview.append({'name': student_name, 'status': 'skipped', 'reason': reason})
                q.put({'type': 'progress', 'current': i + 1, 'total': total,
                       'name': student_name, 'status': 'skipped', 'reason': reason})
                continue

        if essay_text is None:
            skipped += 1
            preview.append({'name': student_name, 'status': 'skipped', 'reason': 'No readable content'})
            q.put({'type': 'progress', 'current': i + 1, 'total': total,
                   'name': student_name, 'status': 'skipped', 'reason': 'No readable content'})
            continue

        essays.append({
            'filename': student_name,
            'text': essay_text,
            'canvas_user_id': user_id,
            'canvas_student_name': student_name,
            'canvas_source_file': source_filename,
        })
        preview.append({'name': student_name, 'status': 'ready', 'file': source_filename})
        q.put({'type': 'progress', 'current': i + 1, 'total': total,
               'name': student_name, 'status': 'ready'})

    if not essays:
        q.put({'type': 'error', 'message': f'No readable submissions found. {skipped} student(s) have not submitted or used unsupported file types.'})
        task['status'] = 'error'
        return

    prefetch_id = str(uuid.uuid4())
    canvas_prefetch[prefetch_id] = {
        'essays': essays,
        'canvas_url': canvas_url,
        'canvas_token': canvas_token,
        'course_id': int(course_id),
        'assignment_id': int(assignment_id),
        'points_possible': points_possible,
        'expires': time.time() + 3600,
    }

    task['status'] = 'complete'
    q.put({
        'type': 'complete',
        'prefetch_id': prefetch_id,
        'count': len(essays),
        'skipped': skipped,
        'preview': preview,
        'points_possible': points_possible,
    })


@app.route('/api/canvas/push-grade', methods=['POST'])
def canvas_push_grade():
    data = request.get_json()
    session_id = (data.get('session_id') or '').strip()
    idx = data.get('idx')
    feedback = (data.get('feedback') or '').strip()

    sess = _get_session(session_id)
    if not sess:
        return jsonify({'error': 'Session not found.'}), 404
    if not sess.get('canvas_enabled'):
        return jsonify({'error': 'This session was not graded from Canvas.'}), 400

    if idx is None or idx < 0 or idx >= len(sess['results']):
        return jsonify({'error': 'Invalid result index.'}), 400

    result = sess['results'][idx]
    if result.get('error'):
        return jsonify({'error': 'Cannot push a result that has a grading error.'}), 400

    canvas_user_id = result.get('canvas_user_id')
    if not canvas_user_id:
        return jsonify({'error': 'No Canvas user ID for this submission.'}), 400

    # Use teacher-edited score if provided, otherwise fall back to AI score
    override_score = data.get('score')
    ai_score = result['score']
    base_score = float(override_score) if override_score is not None else ai_score

    # Scale from rubric points to Canvas assignment points if they differ
    ai_max = result.get('max_score') or 100
    points_possible = sess.get('canvas_points_possible')

    if points_possible and ai_max and ai_max != points_possible:
        canvas_score = round((base_score / ai_max) * points_possible, 2)
    else:
        canvas_score = base_score

    try:
        client = CanvasClient(sess['canvas_url'], sess['canvas_token'])
        client.post_grade(
            course_id=sess['canvas_course_id'],
            assignment_id=sess['canvas_assignment_id'],
            user_id=canvas_user_id,
            score=canvas_score,
            comment=feedback,
        )
        # Track push status server-side so it persists across saves
        if 'push_status' not in sess:
            sess['push_status'] = {}
        sess['push_status'][str(idx)] = 'pushed'
        return jsonify({'success': True, 'canvas_score': canvas_score})
    except CanvasError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {e}'}), 500


# ── Session persistence ───────────────────────────────────────────────────────

def _session_label(sess: dict) -> str:
    total      = sess.get('total', 0)
    model_lbl  = sess.get('model_label', '')
    date_str   = datetime.now().strftime('%b %d, %Y %I:%M %p')
    prefix     = 'Canvas' if sess.get('canvas_enabled') else f'{total} essays'
    students   = f' — {total} students' if sess.get('canvas_enabled') else ''
    return f"{prefix}{students} · {model_lbl} · {date_str}"


@app.route('/api/session/save', methods=['POST'])
def api_session_save():
    data = request.get_json()
    session_id       = (data.get('session_id') or '').strip()
    edited_scores    = data.get('edited_scores', {})
    # Per-level feedback: { "1": { "0": "text" }, "2": { "0": "text" }, "3": { "0": "text" } }
    edited_feedbacks = data.get('edited_feedbacks', {})
    push_status      = data.get('push_status', {})

    sess = _get_session(session_id)
    if not sess:
        return jsonify({'error': 'Session not found.'}), 404

    # Merge teacher edits into results snapshot
    merged = []
    for i, r in enumerate(sess['results']):
        mr = dict(r)
        s_key = str(i)
        if s_key in edited_scores:
            mr['edited_score'] = edited_scores[s_key]
        # Collect per-level feedback edits for this result index
        ef = {}
        for level_key, level_edits in edited_feedbacks.items():
            if s_key in level_edits:
                ef[level_key] = level_edits[s_key]
        if ef:
            mr['edited_feedbacks'] = ef
        merged.append(mr)

    # Persist essay texts so "Show Essay" works on restored sessions
    saved_essays = [
        {'filename': e.get('filename', ''), 'text': e.get('text', '')}
        for e in sess.get('essays', [])
    ]

    save_data = {
        'session_id':      session_id,
        'saved_at':        datetime.now().isoformat(),
        'label':           _session_label(sess),
        'model':           sess.get('model', ''),
        'model_label':     sess.get('model_label', ''),
        'strictness':      sess.get('strictness', DEFAULT_STRICTNESS),
        'strictness_label': sess.get('strictness_label', ''),
        'strictness_emoji': sess.get('strictness_emoji', ''),
        'rubric':          sess.get('rubric', ''),
        'canvas_enabled':  sess.get('canvas_enabled', False),
        'canvas_url':      sess.get('canvas_url', ''),
        # canvas_token intentionally excluded — never write tokens to disk
        'total':           sess.get('total', 0),
        'results':         merged,
        'essays':          saved_essays,
        'push_status':     push_status,
    }

    try:
        session_store.save_session(session_id, save_data)
        return jsonify({'success': True, 'saved_at': save_data['saved_at']})
    except Exception as e:
        return jsonify({'error': f'Could not save session: {e}'}), 500


@app.route('/api/session/list')
def api_session_list():
    return jsonify({'sessions': session_store.list_sessions()})


@app.route('/api/session/load/<session_id>')
def api_session_load(session_id):
    save_data = session_store.load_session(session_id)
    if not save_data:
        return jsonify({'error': 'Saved session not found.'}), 404

    # Apply teacher score edits; feedback edits are passed through as-is
    # (the frontend will pick them up from edited_feedbacks per result)
    results = []
    for r in save_data.get('results', []):
        mr = dict(r)
        if 'edited_score' in r:
            mr['score'] = r['edited_score']
        results.append(mr)

    # Reconstruct a complete (read-only) in-memory session
    sessions[session_id] = {
        'status':           'complete',
        'essays':           save_data.get('essays', []),
        'rubric':           save_data.get('rubric', ''),
        'strictness':       save_data.get('strictness', DEFAULT_STRICTNESS),
        'strictness_label': save_data.get('strictness_label', ''),
        'strictness_emoji': save_data.get('strictness_emoji', ''),
        'model':            save_data.get('model', ''),
        'model_label':      save_data.get('model_label', ''),
        'results':          results,
        'progress':         queue.Queue(),
        'total':            save_data.get('total', len(results)),
        'current':          len(results),
        # Canvas enabled flag present but token absent → push is disabled
        'canvas_enabled':   save_data.get('canvas_enabled', False),
        'canvas_url':       save_data.get('canvas_url', ''),
        'canvas_token':     '',
        'canvas_course_id':     None,
        'canvas_assignment_id': None,
        'canvas_points_possible': None,
        # Restore which students have already been pushed to Canvas
        'push_status':      save_data.get('push_status', {}),
    }

    return jsonify({
        'success': True,
        'session_id': session_id,
        'label': save_data.get('label', ''),
    })


@app.route('/api/session/delete/<session_id>', methods=['DELETE'])
def api_session_delete(session_id):
    if session_store.delete_session(session_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Session not found.'}), 404


# ── Rubric generation ─────────────────────────────────────────────────────────

@app.route('/api/generate-rubric', methods=['POST'])
def generate_rubric():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request.'}), 400

    provider = data.get('provider', '').strip()
    api_key  = data.get('api_key', '').strip()
    model    = data.get('model', '').strip()
    description = data.get('description', '').strip()

    if not provider or provider not in ('claude', 'openai', 'gemini'):
        return jsonify({'error': 'Please select a provider first.'}), 400
    if not api_key:
        return jsonify({'error': 'Please enter your API key first.'}), 400
    if not description:
        return jsonify({'error': 'Please describe the rubric you want.'}), 400

    resolved_model = model if model in ALLOWED_MODELS.get(provider, {}) \
        else DEFAULT_MODELS.get(provider, '')

    try:
        grader = GraderAI(provider, api_key, model=resolved_model)
        prompt = build_rubric_generation_prompt(description)

        if provider == 'claude':
            response = grader.client.messages.create(
                model=grader.model, max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            rubric_text = response.content[0].text.strip()
        elif provider == 'openai':
            response = grader.client.chat.completions.create(
                model=grader.model, max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            rubric_text = response.choices[0].message.content.strip()
        else:  # gemini
            gmodel = grader.client.GenerativeModel(model_name=grader.model)
            response = gmodel.generate_content(prompt)
            rubric_text = response.text.strip()

        return jsonify({'rubric': rubric_text})

    except GradingError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        err = str(e).lower()
        if 'authentication' in err or 'unauthorized' in err or '401' in str(e) \
                or 'api_key' in err or 'permission' in err or '403' in str(e):
            return jsonify({'error': 'Invalid API key. Please check your key.'}), 401
        return jsonify({'error': f'Generation failed: {e}'}), 500


# ── Other routes ──────────────────────────────────────────────────────────────

@app.route('/processing')
def processing():
    return render_template('processing.html')


@app.route('/grade/status')
def grade_status():
    session_id = request.args.get('session_id') or session.get('session_id')
    if not session_id or session_id not in sessions:
        return jsonify({'error': 'No active grading session.'}), 404

    def stream():
        sess = sessions[session_id]
        while True:
            try:
                msg = sess['progress'].get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg['type'] in ('complete', 'error') and msg.get('fatal'):
                    break
                if msg['type'] == 'complete':
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(stream(), mimetype='text/event-stream')


@app.route('/results')
def results():
    return render_template('results.html')


@app.route('/api/results')
def api_results():
    session_id = request.args.get('session_id') or session.get('session_id')
    sess = _get_session(session_id) if session_id else None
    if not sess:
        return jsonify({'error': 'No grading session found.'}), 404
    return jsonify({
        'status': sess['status'],
        'total': sess['total'],
        'results': sess['results'],
        'strictness': sess.get('strictness', DEFAULT_STRICTNESS),
        'strictness_label': sess.get('strictness_label', ''),
        'strictness_emoji': sess.get('strictness_emoji', ''),
        'model': sess.get('model', ''),
        'model_label': sess.get('model_label', ''),
        # Canvas metadata (only present if graded via Canvas)
        'canvas_enabled': sess.get('canvas_enabled', False),
        'canvas_url': sess.get('canvas_url', ''),
        'canvas_points_possible': sess.get('canvas_points_possible'),
        'push_status': sess.get('push_status', {}),
    })


@app.route('/api/results/<int:idx>')
def api_result_single(idx):
    session_id = request.args.get('session_id') or session.get('session_id')
    sess = _get_session(session_id) if session_id else None
    if not sess:
        return jsonify({'error': 'No grading session found.'}), 404
    if idx < 0 or idx >= len(sess['results']):
        return jsonify({'error': 'Invalid index.'}), 404
    return jsonify(sess['results'][idx])


@app.route('/report')
def report():
    session_id = request.args.get('session_id')
    sess = _get_session(session_id) if session_id else None
    if not sess:
        return redirect('/')

    results_list = sess.get('results', [])

    # Compute stats
    graded = [r for r in results_list if not r.get('error')]
    errors = [r for r in results_list if r.get('error')]
    scores = [r['score'] for r in graded if r.get('score') is not None]

    avg_score = f"{sum(scores) / len(scores):.1f}" if scores else 'N/A'
    high_score = f"{max(scores)}" if scores else 'N/A'
    low_score = f"{min(scores)}" if scores else 'N/A'

    # Grade distribution (A=90+, B=80-89, C=70-79, D=60-69, F=<60)
    dist = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'F': 0}
    for r in graded:
        if r.get('score') is not None and r.get('max_score'):
            pct = (r['score'] / r['max_score']) * 100
            if pct >= 90: dist['A'] += 1
            elif pct >= 80: dist['B'] += 1
            elif pct >= 70: dist['C'] += 1
            elif pct >= 60: dist['D'] += 1
            else: dist['F'] += 1

    total_for_dist = max(len(graded), 1)
    distribution = [
        {'letter': letter, 'count': count, 'pct': round(count / total_for_dist * 100)}
        for letter, count in dist.items()
    ]

    # Attention items
    attention_items = [{'filename': r['filename'], 'reason': r['error']} for r in errors]

    # All results for the table
    all_results = []
    for r in results_list:
        row = dict(r)
        if not r.get('error') and r.get('score') is not None and r.get('max_score'):
            row['pct'] = f"{(r['score'] / r['max_score']) * 100:.1f}"
            summary = r.get('summary', '') or ''
            row['summary_short'] = summary[:200] + ('...' if len(summary) > 200 else '')
        all_results.append(row)

    return render_template('report.html',
        session_id=session_id,
        strictness=sess.get('strictness', DEFAULT_STRICTNESS),
        strictness_label=sess.get('strictness_label', ''),
        strictness_emoji=sess.get('strictness_emoji', ''),
        model_label=sess.get('model_label', ''),
        canvas_enabled=sess.get('canvas_enabled', False),
        date=datetime.now().strftime('%B %d, %Y %I:%M %p'),
        total_graded=len(graded),
        total_errors=len(errors),
        avg_score=avg_score,
        high_score=high_score,
        low_score=low_score,
        distribution=distribution,
        attention_items=attention_items,
        all_results=all_results,
        results_json=json.dumps(results_list),
    )


@app.route('/api/regrade', methods=['POST'])
def api_regrade():
    """Re-grade a single student essay at a different strictness level."""
    data = request.get_json()
    session_id = (data.get('session_id') or '').strip()
    idx = data.get('idx')
    new_strictness = data.get('strictness')

    sess = _get_session(session_id)
    if not sess:
        return jsonify({'error': 'Session not found.'}), 404

    if idx is None or idx < 0 or idx >= len(sess.get('essays', [])):
        return jsonify({'error': 'Invalid essay index.'}), 400

    try:
        new_strictness = int(new_strictness)
        new_strictness = max(2, min(10, new_strictness))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid strictness value.'}), 400

    essay = sess['essays'][idx]
    if not essay.get('text'):
        return jsonify({'error': 'No essay text available to re-grade.'}), 400

    # We need provider + API key + model to re-grade.
    # Pull the model from session; provider/key must be sent from the frontend
    # (they were stored in localStorage, not on the server for security).
    provider = (data.get('provider') or '').strip()
    api_key = (data.get('api_key') or '').strip()
    model = data.get('model') or sess.get('model', '')

    if not provider or not api_key:
        return jsonify({'error': 'Provider and API key are required for re-grading.'}), 400

    rubric = sess.get('rubric', '')
    if not rubric:
        return jsonify({'error': 'Original rubric not found in session. Please re-grade from a fresh batch.'}), 400

    try:
        grader = GraderAI(provider, api_key, model=model)
        graded = grader.grade_essay(rubric, essay['text'], essay['filename'], new_strictness)
        result = {
            'filename': essay['filename'],
            'score': graded['score'],
            'max_score': graded['max_score'],
            'summary': graded['summary'],
            'categories': graded.get('categories', []),
            'error': None,
        }
        # Carry over Canvas user ID if present
        if essay.get('canvas_user_id'):
            result['canvas_user_id'] = essay['canvas_user_id']

        # Update the in-memory result
        sess['results'][idx] = result

        strictness_info = get_strictness_info(new_strictness)

        return jsonify({
            'success': True,
            'result': result,
            'strictness': new_strictness,
            'strictness_label': strictness_info['label'],
            'strictness_emoji': strictness_info['emoji'],
        })

    except GradingError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        err = str(e).lower()
        if 'authentication' in err or 'unauthorized' in err or '401' in str(e):
            return jsonify({'error': 'Invalid API key. Please check your key.'}), 401
        return jsonify({'error': f'Re-grading failed: {e}'}), 500


@app.route('/api/session/add-essays', methods=['POST'])
def api_add_essays():
    """Add and grade additional essay files into an existing session."""
    session_id = request.form.get('session_id', '').strip()
    provider = request.form.get('provider', '').strip()
    api_key = request.form.get('api_key', '').strip()

    sess = _get_session(session_id)
    if not sess:
        return jsonify({'error': 'Session not found.'}), 404

    if not provider or not api_key:
        return jsonify({'error': 'Provider and API key required.'}), 400

    rubric = sess.get('rubric', '')
    if not rubric:
        return jsonify({'error': 'Original rubric not found in session.'}), 400

    strictness = sess.get('strictness', DEFAULT_STRICTNESS)
    model = sess.get('model', '')

    # Extract uploaded files
    uploaded_files = request.files.getlist('essay_files')
    if not uploaded_files or all(not f.filename for f in uploaded_files):
        return jsonify({'error': 'Please select at least one essay file.'}), 400

    new_essays = []
    for f in sorted(uploaded_files, key=lambda x: x.filename or ''):
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in SUPPORTED_ESSAY_EXTENSIONS:
            continue
        try:
            file_bytes = f.read()
            text = extract_text(f.filename, file_bytes)
            new_essays.append({'filename': os.path.basename(f.filename), 'text': text})
        except ExtractionError as e:
            new_essays.append({
                'filename': os.path.basename(f.filename),
                'text': None, 'error': str(e),
            })

    if not new_essays:
        return jsonify({'error': 'No valid essay files found. Use .docx, .pdf, or .txt.'}), 400

    # Grade each new essay
    try:
        grader = GraderAI(provider, api_key, model=model)
    except GradingError as e:
        return jsonify({'error': str(e)}), 400

    new_results = []
    for essay in new_essays:
        if essay.get('error'):
            result = {
                'filename': essay['filename'],
                'score': None, 'max_score': None,
                'summary': None, 'error': essay['error'],
            }
        else:
            try:
                graded = grader.grade_essay(rubric, essay['text'], essay['filename'], strictness)
                result = {
                    'filename': essay['filename'],
                    'score': graded['score'],
                    'max_score': graded['max_score'],
                    'summary': graded['summary'],
                    'categories': graded.get('categories', []),
                    'error': None,
                }
            except (GradingError, Exception) as e:
                result = {
                    'filename': essay['filename'],
                    'score': None, 'max_score': None,
                    'summary': None, 'error': str(e),
                }
        new_results.append(result)

    # Append to the existing session
    sess['essays'].extend(new_essays)
    sess['results'].extend(new_results)
    sess['total'] = len(sess['results'])

    return jsonify({
        'success': True,
        'added': len(new_results),
        'new_total': sess['total'],
    })


@app.route('/api/essay-text/<int:idx>')
def api_essay_text(idx):
    session_id = request.args.get('session_id') or session.get('session_id')
    sess = _get_session(session_id) if session_id else None
    if not sess:
        return jsonify({'error': 'No grading session found.'}), 404

    essays = sess.get('essays', [])
    if not essays:
        return jsonify({'error': 'Essay text is not available. This session was saved before essay storage was added — re-grade to enable Show Essay.'}), 404
    if idx < 0 or idx >= len(essays):
        return jsonify({'error': 'Essay not found.'}), 404
    essay = essays[idx]
    text = essay.get('text') or ''
    if not text:
        return jsonify({'error': 'Essay text is not available for this submission.'}), 404
    return jsonify({'filename': essay.get('filename', ''), 'text': text})


if __name__ == '__main__':
    import webbrowser, threading
    port = int(os.environ.get('PORT', 5050))
    print("\n  Wolverine Grader Pro 3.0")
    print(f"  Open http://127.0.0.1:{port} in your browser\n")
    # Auto-open the browser after a short delay so the server is ready
    threading.Timer(1.2, lambda: webbrowser.open(f'http://127.0.0.1:{port}')).start()
    app.run(host='127.0.0.1', debug=False, port=port, threaded=True)
