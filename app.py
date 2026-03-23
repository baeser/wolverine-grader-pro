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
from grader.ai_client import (GraderAI, GradingError, ALLOWED_MODELS, DEFAULT_MODELS,
                              MODEL_LABELS, get_review_model)
from grader.batch_client import BatchGrader, BatchError
from grader.canvas_client import CanvasClient, CanvasError
from grader.extractor import ExtractionError, extract_text
from grader.prompt_builder import (get_strictness_info, DEFAULT_STRICTNESS,
                                   DEFAULT_TONE, get_tone_info,
                                   build_rubric_generation_prompt, build_review_prompt,
                                   build_system_prompt, build_essay_message,
                                   build_quiz_system_prompt, build_quiz_message)
from grader import session_store
from grader import batch_store
from grader.license_manager import get_license_manager, TRIAL_MAX_SESSIONS

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


# ── License routes ─────────────────────────────────────────────────────────────

@app.route('/activate')
def activate_page():
    lm = get_license_manager()
    status = lm.get_status()
    # Show trial_expired as distinct state
    if lm.is_trial_expired() and not lm.is_licensed():
        status['status'] = 'trial_expired'
        status['max_sessions'] = TRIAL_MAX_SESSIONS
    return render_template('activate.html',
                           license_status=status,
                           trial_max=TRIAL_MAX_SESSIONS)


@app.route('/api/license/status')
def api_license_status():
    lm = get_license_manager()
    return jsonify(lm.get_status())


@app.route('/api/license/start-trial', methods=['POST'])
def api_license_start_trial():
    lm = get_license_manager()
    result = lm.start_trial()
    return jsonify(result)


@app.route('/api/license/claim', methods=['POST'])
def api_license_claim():
    data = request.get_json()
    order_number = (data.get('order_number') or '').strip()
    buyer_name = (data.get('buyer_name') or '').strip()
    buyer_email = (data.get('buyer_email') or '').strip()
    if not order_number or not buyer_name:
        return jsonify({'error': 'Order number and name are required.'}), 400
    lm = get_license_manager()
    result = lm.claim_purchase(order_number, buyer_name, buyer_email)
    if result.get('error'):
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/license/activate', methods=['POST'])
def api_license_activate():
    data = request.get_json()
    license_key = (data.get('license_key') or '').strip()
    if not license_key:
        return jsonify({'error': 'License key is required.'}), 400
    lm = get_license_manager()
    result = lm.activate_device(license_key)
    if result.get('error'):
        code = 403 if 'limit' in result.get('error', '').lower() else 400
        return jsonify(result), code
    return jsonify(result)


@app.route('/api/license/deactivate', methods=['POST'])
def api_license_deactivate():
    lm = get_license_manager()
    result = lm.deactivate_device()
    if result.get('error'):
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/license/validate', methods=['POST'])
def api_license_validate():
    lm = get_license_manager()
    result = lm.validate_online()
    return jsonify(result)


@app.route('/api/license/check-update')
def api_license_check_update():
    lm = get_license_manager()
    update = lm.check_for_updates()
    return jsonify({'update': update})


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    lm = get_license_manager()
    if not lm.can_grade():
        return redirect(url_for('activate_page'))
    return render_template('index.html', license_status=lm.get_status())


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

    # Feedback tone
    tone = request.form.get('tone', DEFAULT_TONE).strip() or DEFAULT_TONE
    custom_phrases = request.form.get('custom_phrases', '').strip() or None
    tone_info = get_tone_info(tone)

    essay_mode = request.form.get('essay_mode', 'zip').strip()
    grading_mode = request.form.get('grading_mode', 'essay').strip()

    # Validate inputs
    if not provider or provider not in ('claude', 'openai', 'gemini'):
        return jsonify({'error': 'Please select an AI provider.'}), 400
    if not api_key:
        return jsonify({'error': 'Please enter your API key.'}), 400
    if essay_mode not in ('zip', 'files', 'canvas'):
        return jsonify({'error': 'Invalid essay upload mode.'}), 400
    if grading_mode not in ('essay', 'quiz'):
        return jsonify({'error': 'Invalid grading mode.'}), 400

    # Extract rubric
    if rubric_file and rubric_file.filename:
        ext = os.path.splitext(rubric_file.filename)[1].lower()
        if ext not in SUPPORTED_RUBRIC_EXTENSIONS:
            return jsonify({'error': f'Unsupported rubric file type: {ext}'}), 400
        try:
            rubric_text = extract_text(rubric_file.filename, rubric_file.read())
        except ExtractionError as e:
            return jsonify({'error': f'Could not read rubric: {e}'}), 400

    if not rubric_text and grading_mode != 'quiz':
        return jsonify({'error': 'Please provide a rubric (paste text or upload a file).'}), 400

    # ── Quiz mode — use pre-fetched quiz data ─────────────────────────────────
    if grading_mode == 'quiz':
        prefetch_id = request.form.get('canvas_prefetch_id', '').strip()
        _cleanup_canvas_prefetch()
        prefetch = canvas_prefetch.get(prefetch_id)
        if not prefetch or prefetch.get('grading_mode') != 'quiz':
            return jsonify({'error': 'Quiz session expired. Please fetch submissions again.'}), 400

        quiz_questions = prefetch['questions']
        quiz_submissions = prefetch['submissions']
        answer_key = request.form.get('quiz_answer_key', '').strip() or None

        if not quiz_submissions:
            return jsonify({'error': 'No quiz submissions to grade.'}), 400

        canvas_meta = {
            'canvas_enabled': True,
            'canvas_url': prefetch['canvas_url'],
            'canvas_token': prefetch['canvas_token'],
            'canvas_course_id': prefetch['course_id'],
            'canvas_quiz_id': prefetch['quiz_id'],
            'canvas_points_possible': prefetch.get('points_possible'),
            'quiz_type': prefetch.get('quiz_type', 'classic'),
        }

        # License check
        lm = get_license_manager()
        if not lm.can_grade():
            return jsonify({'error': 'License required. Please activate or start a trial.'}), 403
        if lm.is_trial() and not lm.use_trial_session():
            return jsonify({'error': 'Trial sessions exhausted. Please purchase a license.'}), 403

        # Create session
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id
        strictness_info = get_strictness_info(strictness)
        model_label = MODEL_LABELS.get(model, model)
        sess_data = {
            'status': 'grading',
            'grading_mode': 'quiz',
            'quiz_questions': quiz_questions,
            'essays': [],  # not used for quiz mode
            'rubric': rubric_text or '',
            'answer_key': answer_key,
            'strictness': strictness,
            'strictness_label': strictness_info['label'],
            'strictness_emoji': strictness_info['emoji'],
            'tone': tone,
            'tone_label': tone_info['label'],
            'tone_emoji': tone_info['emoji'],
            'custom_phrases': custom_phrases or '',
            'model': model,
            'model_label': model_label,
            'results': [],
            'progress': queue.Queue(),
            'total': len(quiz_submissions),
            'current': 0,
            'calibration_examples': [],
        }
        sess_data.update(canvas_meta)
        sessions[session_id] = sess_data

        thread = threading.Thread(
            target=_grade_quiz_submissions,
            args=(session_id, provider, api_key, quiz_questions,
                  quiz_submissions, strictness, model, answer_key,
                  tone, custom_phrases,
                  prefetch.get('points_possible')),
            daemon=True,
        )
        thread.start()

        return jsonify({
            'session_id': session_id,
            'total': len(quiz_submissions),
            'strictness': strictness,
            'strictness_label': strictness_info['label'],
            'strictness_emoji': strictness_info['emoji'],
            'model': model,
            'model_label': model_label,
        })

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

    # ── Extract calibration exemplars (optional) ────────────────────────────
    calibration_examples = []
    for tag, label in [('strong', 'Strong Example (A-level work)'),
                       ('weak', 'Needs Improvement (below expectations)')]:
        cal_file = request.files.get(f'cal_{tag}_file')
        cal_score = request.form.get(f'cal_{tag}_score', '').strip()
        cal_max = request.form.get(f'cal_{tag}_max', '').strip()
        cal_feedback = request.form.get(f'cal_{tag}_feedback', '').strip()

        if cal_file and cal_file.filename and cal_score and cal_feedback:
            try:
                cal_text = extract_text(cal_file.filename, cal_file.read())
                cal_score_val = float(cal_score)
                cal_max_val = float(cal_max) if cal_max else 100.0
                calibration_examples.append({
                    'label': label,
                    'text': cal_text,
                    'score': cal_score_val,
                    'max_score': cal_max_val,
                    'feedback': cal_feedback,
                })
            except (ExtractionError, ValueError):
                pass  # skip malformed calibration — non-blocking

    # ── License check ────────────────────────────────────────────────────────
    lm = get_license_manager()
    if not lm.can_grade():
        return jsonify({'error': 'License required. Please activate or start a trial.'}), 403
    if lm.is_trial() and not lm.use_trial_session():
        return jsonify({'error': 'Trial sessions exhausted. Please purchase a license.'}), 403

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
        'tone': tone,
        'tone_label': tone_info['label'],
        'tone_emoji': tone_info['emoji'],
        'custom_phrases': custom_phrases or '',
        'model': model,
        'model_label': model_label,
        'results': [],
        'progress': queue.Queue(),
        'total': len(essays),
        'current': 0,
        'calibration_examples': calibration_examples or [],
    }
    sess_data.update(canvas_meta)
    sessions[session_id] = sess_data

    thread = threading.Thread(
        target=_grade_essays,
        args=(session_id, provider, api_key, rubric_text, essays, strictness, model,
              calibration_examples or None, tone, custom_phrases),
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
                  strictness=DEFAULT_STRICTNESS, model=None,
                  calibration_examples=None,
                  tone=DEFAULT_TONE, custom_phrases=None):
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
                graded = grader.grade_essay(rubric, essay['text'], essay['filename'],
                                            strictness,
                                            calibration_examples=calibration_examples,
                                            tone=tone, custom_phrases=custom_phrases)
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


# ── Canvas Quiz routes ────────────────────────────────────────────────────────

@app.route('/api/canvas/quizzes')
def canvas_quizzes():
    canvas_url = request.args.get('canvas_url', '').strip()
    canvas_token = request.args.get('canvas_token', '').strip()
    course_id = request.args.get('course_id', '').strip()
    if not all([canvas_url, canvas_token, course_id]):
        return jsonify({'error': 'canvas_url, canvas_token, and course_id are required.'}), 400
    try:
        client = CanvasClient(canvas_url, canvas_token)
        quizzes = client.get_quizzes(int(course_id))
        return jsonify({'quizzes': quizzes})
    except CanvasError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {e}'}), 500


@app.route('/api/canvas/quiz-questions')
def canvas_quiz_questions():
    canvas_url = request.args.get('canvas_url', '').strip()
    canvas_token = request.args.get('canvas_token', '').strip()
    course_id = request.args.get('course_id', '').strip()
    quiz_id = request.args.get('quiz_id', '').strip()
    quiz_type = request.args.get('quiz_type', 'classic').strip()
    if not all([canvas_url, canvas_token, course_id, quiz_id]):
        return jsonify({'error': 'canvas_url, canvas_token, course_id, and quiz_id are required.'}), 400
    try:
        client = CanvasClient(canvas_url, canvas_token)

        if quiz_type == 'new':
            # New Quizzes — use the items API
            questions = client.get_new_quiz_items(int(course_id), int(quiz_id))
        else:
            questions = client.get_quiz_questions(int(course_id), int(quiz_id))
        # Count gradable question types
        gradable_types = ('essay_question', 'short_answer_question')
        gradable = [q for q in questions if q['question_type'] in gradable_types]
        auto_graded = [q for q in questions if q['question_type'] not in gradable_types]
        return jsonify({
            'questions': questions,
            'gradable': gradable,
            'auto_graded_count': len(auto_graded),
            'gradable_count': len(gradable),
        })
    except CanvasError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {e}'}), 500


@app.route('/api/canvas/start-quiz-fetch', methods=['POST'])
def canvas_start_quiz_fetch():
    data = request.get_json()
    canvas_url = (data.get('canvas_url') or '').strip()
    canvas_token = (data.get('canvas_token') or '').strip()
    course_id = data.get('course_id')
    quiz_id = data.get('quiz_id')
    points_possible = data.get('points_possible')
    quiz_type = data.get('quiz_type', 'classic')

    if not all([canvas_url, canvas_token, course_id, quiz_id]):
        return jsonify({'error': 'Missing required fields.'}), 400

    fetch_id = str(uuid.uuid4())
    canvas_fetch_tasks[fetch_id] = {
        'status': 'running',
        'queue': queue.Queue(),
    }

    if quiz_type == 'new':
        thread = threading.Thread(
            target=_do_canvas_new_quiz_fetch,
            args=(fetch_id, canvas_url, canvas_token, course_id, quiz_id, points_possible),
            daemon=True,
        )
    else:
        thread = threading.Thread(
            target=_do_canvas_quiz_fetch,
            args=(fetch_id, canvas_url, canvas_token, course_id, quiz_id, points_possible),
            daemon=True,
        )
    thread.start()

    return jsonify({'fetch_id': fetch_id})


def _do_canvas_quiz_fetch(fetch_id, canvas_url, canvas_token, course_id, quiz_id, points_possible):
    """Background thread: fetch quiz questions + student answers from Canvas."""
    import re as _re
    task = canvas_fetch_tasks.get(fetch_id)
    if not task:
        return
    q = task['queue']

    try:
        client = CanvasClient(canvas_url, canvas_token)
        all_questions = client.get_quiz_questions(int(course_id), int(quiz_id))
        quiz_subs = client.get_quiz_submissions(int(course_id), int(quiz_id))
    except CanvasError as e:
        q.put({'type': 'error', 'message': str(e)})
        task['status'] = 'error'
        return
    except Exception as e:
        q.put({'type': 'error', 'message': f'Error fetching quiz data: {e}'})
        task['status'] = 'error'
        return

    # Filter to gradable question types
    gradable_types = ('essay_question', 'short_answer_question')
    gradable_questions = [qn for qn in all_questions if qn['question_type'] in gradable_types]

    if not gradable_questions:
        q.put({'type': 'error', 'message': 'This quiz has no essay or short answer questions to grade.'})
        task['status'] = 'error'
        return

    # Build question lookup: {question_id: question_data}
    gradable_ids = {qn['id'] for qn in gradable_questions}
    question_map = {qn['id']: qn for qn in gradable_questions}

    # Process quiz submissions — Canvas may return duplicates for multiple attempts
    # Keep only the latest attempt per user
    latest_by_user = {}
    for sub in quiz_subs:
        user_id = sub.get('user_id')
        attempt = sub.get('attempt', 1)
        if user_id not in latest_by_user or attempt > latest_by_user[user_id].get('attempt', 0):
            latest_by_user[user_id] = sub

    subs_to_process = list(latest_by_user.values())
    total = len(subs_to_process)
    q.put({'type': 'start', 'total': total})

    submissions = []
    preview = []
    skipped = 0

    for i, sub in enumerate(subs_to_process):
        user = sub.get('user') or {}
        student_name = user.get('name') or user.get('login_id') or f"Student {sub.get('user_id', '?')}"
        user_id = sub.get('user_id')
        quiz_sub_id = sub.get('id')
        attempt = sub.get('attempt', 1)

        if not quiz_sub_id:
            skipped += 1
            q.put({'type': 'progress', 'current': i + 1, 'total': total,
                   'name': student_name, 'status': 'skipped', 'reason': 'No submission ID'})
            continue

        # Fetch per-question answers for this submission
        try:
            sub_questions = client.get_quiz_submission_questions(quiz_sub_id)
        except CanvasError:
            skipped += 1
            q.put({'type': 'progress', 'current': i + 1, 'total': total,
                   'name': student_name, 'status': 'skipped', 'reason': 'Could not fetch answers'})
            continue

        # Build answers list for gradable questions only
        answers = []
        for sq in sub_questions:
            qid = sq.get('quiz_question_id') or sq.get('id')
            if qid not in gradable_ids:
                continue
            qinfo = question_map[qid]

            # Extract answer text — Canvas stores essay answers in 'answer' field
            answer_text = ''
            raw_answer = sq.get('answer')
            if isinstance(raw_answer, str):
                # Strip HTML tags from rich-text answers
                answer_text = _re.sub(r'<[^>]+>', ' ', raw_answer).strip()
            elif isinstance(raw_answer, dict):
                answer_text = raw_answer.get('text', '')
            elif raw_answer is None:
                answer_text = ''

            answers.append({
                'question_id': str(qid),
                'question_text': _re.sub(r'<[^>]+>', ' ', qinfo.get('question_text', '')).strip(),
                'answer_text': answer_text,
                'points': qinfo.get('points_possible', 0),
            })

        if not answers:
            skipped += 1
            preview.append({'name': student_name, 'status': 'skipped', 'reason': 'No gradable answers'})
            q.put({'type': 'progress', 'current': i + 1, 'total': total,
                   'name': student_name, 'status': 'skipped', 'reason': 'No gradable answers'})
            continue

        submissions.append({
            'filename': student_name,
            'canvas_user_id': user_id,
            'quiz_submission_id': quiz_sub_id,
            'attempt': attempt,
            'answers': answers,
        })
        preview.append({'name': student_name, 'status': 'ready'})
        q.put({'type': 'progress', 'current': i + 1, 'total': total,
               'name': student_name, 'status': 'ready'})

    if not submissions:
        q.put({'type': 'error', 'message': f'No gradable submissions found. {skipped} skipped.'})
        task['status'] = 'error'
        return

    # Build question list for the grading prompt
    quiz_q_list = [
        {
            'id': str(qn['id']),
            'text': _re.sub(r'<[^>]+>', ' ', qn.get('question_text', '')).strip(),
            'points': qn.get('points_possible', 0),
            'question_type': qn['question_type'],
        }
        for qn in gradable_questions
    ]

    prefetch_id = str(uuid.uuid4())
    canvas_prefetch[prefetch_id] = {
        'grading_mode': 'quiz',
        'questions': quiz_q_list,
        'submissions': submissions,
        'canvas_url': canvas_url,
        'canvas_token': canvas_token,
        'course_id': int(course_id),
        'quiz_id': int(quiz_id),
        'points_possible': points_possible,
        'expires': time.time() + 3600,
    }

    task['status'] = 'complete'
    q.put({
        'type': 'complete',
        'prefetch_id': prefetch_id,
        'count': len(submissions),
        'skipped': skipped,
        'preview': preview,
        'points_possible': points_possible,
        'gradable_count': len(gradable_questions),
    })


def _do_canvas_new_quiz_fetch(fetch_id, canvas_url, canvas_token, course_id, quiz_id, points_possible):
    """Background thread: fetch New Quiz questions + student answers via Reports API."""
    import re as _re
    task = canvas_fetch_tasks.get(fetch_id)
    if not task:
        return
    q = task['queue']

    try:
        client = CanvasClient(canvas_url, canvas_token)

        # Step 1: Fetch quiz items (questions)
        all_questions = client.get_new_quiz_items(int(course_id), int(quiz_id))

        # Step 2: Request the Student Analysis report
        progress = client.request_new_quiz_report(int(course_id), int(quiz_id), fmt='json')
        progress_url = progress.get('url', '')
        if not progress_url:
            raise CanvasError('No progress URL returned from report request.')

        # Step 3: Poll until report is ready
        result = client.poll_progress(progress_url, timeout=120)
        results_info = result.get('results', {})
        report_url = results_info.get('url', '')
        if not report_url:
            raise CanvasError('Report completed but no download URL was returned.')

        # Step 4: Download the report
        report_data = client.download_report(report_url)

    except CanvasError as e:
        q.put({'type': 'error', 'message': str(e)})
        task['status'] = 'error'
        return
    except Exception as e:
        q.put({'type': 'error', 'message': f'Error fetching New Quiz data: {e}'})
        task['status'] = 'error'
        return

    # Filter items API questions to gradable types
    gradable_types = ('essay_question', 'short_answer_question')
    gradable_questions = [qn for qn in all_questions if qn['question_type'] in gradable_types]

    if not gradable_questions:
        q.put({'type': 'error', 'message': 'This quiz has no essay or short answer questions to grade.'})
        task['status'] = 'error'
        return

    # Build position-based mapping: items API and report use different IDs
    # but questions are in the same positional order.
    # Map items API question by position, then map report item_id -> items API question.
    items_by_position = {qn.get('position', idx): qn for idx, qn in enumerate(all_questions)}
    gradable_positions = {qn.get('position', idx) for idx, qn in enumerate(all_questions)
                          if qn['question_type'] in gradable_types}

    # Gradable report item_types (report uses 'essay', 'short_answer', etc.)
    gradable_report_types = ('essay', 'short_answer', 'fill_in_the_blank', 'rich_fill_in_the_blank')

    # Build report_item_id -> items_api question mapping using first student's responses
    report_id_to_question = {}
    if report_data:
        first_responses = report_data[0].get('item_responses', [])
        for pos_idx, resp in enumerate(first_responses):
            report_item_id = str(resp.get('item_id', ''))
            # Match by position (1-indexed in items API)
            position = pos_idx + 1
            if position in items_by_position:
                api_question = items_by_position[position]
                if api_question['question_type'] in gradable_types:
                    report_id_to_question[report_item_id] = api_question

    # Process report data — each record is one student
    total = len(report_data)
    q.put({'type': 'start', 'total': total})

    submissions = []
    preview = []
    skipped = 0

    for i, record in enumerate(report_data):
        student = record.get('student_data', {})
        student_name = student.get('name', f"Student {student.get('id', '?')}")
        user_id = student.get('id')
        attempt = student.get('attempt', 1)
        item_responses = record.get('item_responses', [])

        # Build answers list for gradable questions only
        answers = []
        for resp in item_responses:
            report_item_id = str(resp.get('item_id', ''))
            item_type = resp.get('item_type', '')

            # Use the mapping if available, otherwise filter by report item_type
            qinfo = report_id_to_question.get(report_item_id)
            if not qinfo and item_type not in gradable_report_types:
                continue
            if not qinfo:
                # Fallback: match by position within this student's responses
                resp_idx = item_responses.index(resp)
                position = resp_idx + 1
                qinfo = items_by_position.get(position)
                if not qinfo or qinfo['question_type'] not in gradable_types:
                    continue

            # Extract answer text — report stores as HTML
            raw_answer = resp.get('answer', '')
            if isinstance(raw_answer, str) and raw_answer.strip():
                answer_text = _re.sub(r'<[^>]+>', ' ', raw_answer).strip()
                answer_text = _re.sub(r'\s+', ' ', answer_text).strip()
            else:
                answer_text = ''

            answers.append({
                'question_id': str(qinfo['id']),  # Use items API ID for consistency
                'question_text': _re.sub(r'<[^>]+>', ' ', qinfo.get('question_text', '')).strip(),
                'answer_text': answer_text,
                'points': qinfo.get('points_possible', 0),
            })

        if not answers:
            skipped += 1
            preview.append({'name': student_name, 'status': 'skipped', 'reason': 'No gradable answers'})
            q.put({'type': 'progress', 'current': i + 1, 'total': total,
                   'name': student_name, 'status': 'skipped', 'reason': 'No gradable answers'})
            continue

        submissions.append({
            'filename': student_name,
            'canvas_user_id': user_id,
            'quiz_submission_id': None,  # New Quizzes don't use quiz_submission_id
            'attempt': attempt,
            'answers': answers,
            'quiz_type': 'new',
        })
        preview.append({'name': student_name, 'status': 'ready'})
        q.put({'type': 'progress', 'current': i + 1, 'total': total,
               'name': student_name, 'status': 'ready'})

    if not submissions:
        q.put({'type': 'error', 'message': f'No gradable submissions found. {skipped} skipped.'})
        task['status'] = 'error'
        return

    # Build question list for the grading prompt
    quiz_q_list = [
        {
            'id': str(qn['id']),
            'text': _re.sub(r'<[^>]+>', ' ', qn.get('question_text', '')).strip(),
            'points': qn.get('points_possible', 0),
            'question_type': qn['question_type'],
        }
        for qn in gradable_questions
    ]

    prefetch_id = str(uuid.uuid4())
    canvas_prefetch[prefetch_id] = {
        'grading_mode': 'quiz',
        'quiz_type': 'new',
        'questions': quiz_q_list,
        'submissions': submissions,
        'canvas_url': canvas_url,
        'canvas_token': canvas_token,
        'course_id': int(course_id),
        'quiz_id': int(quiz_id),
        'points_possible': points_possible,
        'expires': time.time() + 3600,
    }

    task['status'] = 'complete'
    q.put({
        'type': 'complete',
        'prefetch_id': prefetch_id,
        'count': len(submissions),
        'skipped': skipped,
        'preview': preview,
        'points_possible': points_possible,
        'gradable_count': len(gradable_questions),
    })


def _grade_quiz_submissions(session_id, provider, api_key, questions,
                             submissions, strictness=DEFAULT_STRICTNESS,
                             model=None, answer_key=None,
                             tone=DEFAULT_TONE, custom_phrases=None,
                             points_possible=None):
    """Background thread: grade quiz submissions with AI."""
    sess = sessions.get(session_id)
    if not sess:
        return

    try:
        grader = GraderAI(provider, api_key, model=model)
    except GradingError as e:
        sess['progress'].put({'type': 'error', 'message': str(e), 'fatal': True})
        sess['status'] = 'error'
        return

    for i, submission in enumerate(submissions):
        sess['current'] = i + 1

        try:
            graded = grader.grade_quiz_submission(
                questions, submission['answers'], submission['filename'],
                strictness, answer_key=answer_key,
                tone=tone, custom_phrases=custom_phrases,
                points_possible=points_possible,
            )
            # Enforce known points_possible as max_score (AI may still deviate)
            max_score = graded['max_score']
            if points_possible and points_possible > 0:
                max_score = float(points_possible)

            result = {
                'filename': submission['filename'],
                'score': min(graded['score'], max_score),
                'max_score': max_score,
                'summary': graded['summary'],
                'categories': [],
                'questions': graded.get('questions', []),
                'error': None,
            }
        except (GradingError, Exception) as e:
            result = {
                'filename': submission['filename'],
                'score': None, 'max_score': None,
                'summary': None, 'error': str(e),
            }

        # Carry over Canvas metadata
        if submission.get('canvas_user_id'):
            result['canvas_user_id'] = submission['canvas_user_id']
        if submission.get('quiz_submission_id'):
            result['quiz_submission_id'] = submission['quiz_submission_id']
            result['attempt'] = submission.get('attempt', 1)
        if submission.get('quiz_type'):
            result['quiz_type'] = submission['quiz_type']

        sess['results'].append(result)
        sess['progress'].put({
            'type': 'progress',
            'current': i + 1,
            'total': len(submissions),
            'filename': submission['filename'],
            'status': 'error' if result['error'] else 'done',
        })

    sess['progress'].put({'type': 'complete'})
    sess['status'] = 'complete'


@app.route('/api/canvas/push-quiz-grade', methods=['POST'])
def canvas_push_quiz_grade():
    """Push per-question grades back to a Canvas quiz (Classic or New)."""
    data = request.get_json()
    session_id = (data.get('session_id') or '').strip()
    idx = data.get('idx')

    sess = _get_session(session_id)
    if not sess:
        return jsonify({'error': 'Session not found.'}), 404
    if not sess.get('canvas_enabled') or sess.get('grading_mode') != 'quiz':
        return jsonify({'error': 'This session is not a Canvas quiz session.'}), 400

    if idx is None or idx < 0 or idx >= len(sess['results']):
        return jsonify({'error': 'Invalid result index.'}), 400

    result = sess['results'][idx]
    if result.get('error'):
        return jsonify({'error': 'Cannot push a result that has a grading error.'}), 400

    graded_questions = result.get('questions', [])
    if not graded_questions:
        return jsonify({'error': 'No per-question grades available.'}), 400

    canvas_url = data.get('canvas_url') or sess.get('canvas_url', '')
    canvas_token = data.get('canvas_token') or sess.get('canvas_token', '')

    if not canvas_url or not canvas_token:
        return jsonify({'error': 'Canvas credentials required. Please re-enter your Canvas token.'}), 400

    quiz_type = result.get('quiz_type', sess.get('quiz_type', 'classic'))

    try:
        client = CanvasClient(canvas_url, canvas_token)

        if quiz_type == 'new':
            # New Quizzes — push total score + feedback via assignments API
            user_id = result.get('canvas_user_id')
            course_id = sess.get('canvas_course_id') or sess.get('course_id')
            assignment_id = sess.get('canvas_quiz_id') or sess.get('quiz_id')

            if not all([user_id, course_id, assignment_id]):
                return jsonify({'error': 'Missing Canvas IDs for grade push.'}), 400

            total_score = sum(gq.get('earned', 0) for gq in graded_questions)
            feedback_parts = []
            for gq in graded_questions:
                fb = gq.get('feedback', '').strip()
                if fb:
                    feedback_parts.append(f"Q{gq.get('question_id', '?')}: {gq['earned']}/{gq['possible']} — {fb}")
            comment = '\n'.join(feedback_parts) if feedback_parts else result.get('summary', '')

            client.post_grade(int(course_id), int(assignment_id), int(user_id),
                              total_score, comment)
        else:
            # Classic Quiz — push per-question grades
            quiz_sub_id = result.get('quiz_submission_id')
            attempt = result.get('attempt', 1)
            if not quiz_sub_id:
                return jsonify({'error': 'No quiz submission ID for this result.'}), 400

            push_questions = []
            for gq in graded_questions:
                push_questions.append({
                    'id': int(gq['question_id']),
                    'score': gq['earned'],
                    'comment': gq.get('feedback', ''),
                })
            client.post_quiz_grades(quiz_sub_id, attempt, push_questions)

        if 'push_status' not in sess:
            sess['push_status'] = {}
        sess['push_status'][str(idx)] = 'pushed'
        return jsonify({'success': True})
    except CanvasError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {e}'}), 500


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
    if sess.get('grading_mode') == 'quiz':
        prefix = 'Canvas Quiz'
    elif sess.get('canvas_enabled'):
        prefix = 'Canvas'
    else:
        prefix = f'{total} essays'
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
        'grading_mode':    sess.get('grading_mode', 'essay'),
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
    # Persist quiz-specific data
    if sess.get('grading_mode') == 'quiz':
        save_data['quiz_questions'] = sess.get('quiz_questions', [])

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
    restored = {
        'status':           'complete',
        'grading_mode':     save_data.get('grading_mode', 'essay'),
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
    if save_data.get('grading_mode') == 'quiz':
        restored['quiz_questions'] = save_data.get('quiz_questions', [])
    sessions[session_id] = restored

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
    lm = get_license_manager()
    return render_template('processing.html', license_status=lm.get_status())


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
    lm = get_license_manager()
    return render_template('results.html', license_status=lm.get_status())


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
        'grading_mode': sess.get('grading_mode', 'essay'),
        'quiz_questions': sess.get('quiz_questions', []),
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


# ── Batch Grading ────────────────────────────────────────────────────────────

@app.route('/batch-pending')
def batch_pending():
    lm = get_license_manager()
    return render_template('batch_pending.html', license_status=lm.get_status())


@app.route('/grade/batch', methods=['POST'])
def grade_batch():
    """Submit essays/quiz for batch grading (50% cheaper, async)."""
    provider = request.form.get('provider', '').strip().lower()
    api_key = request.form.get('api_key', '').strip()
    model = request.form.get('model', '').strip()
    strictness = int(request.form.get('strictness', DEFAULT_STRICTNESS))
    grading_mode = request.form.get('grading_mode', 'essay').strip()
    tone = request.form.get('tone', DEFAULT_TONE).strip() or DEFAULT_TONE
    custom_phrases = request.form.get('custom_phrases', '').strip() or None
    tone_info_batch = get_tone_info(tone)

    if provider not in ALLOWED_MODELS:
        return jsonify({'error': 'Invalid AI provider.'}), 400
    if not api_key:
        return jsonify({'error': 'Please enter your API key.'}), 400

    # Gemini doesn't support batch API
    if provider == 'gemini':
        return jsonify({'error': 'Batch grading is available for Claude and OpenAI only. '
                       'Use "Grade Now" for Gemini, or switch providers.'}), 400

    # Validate model
    allowed = ALLOWED_MODELS[provider]
    default = DEFAULT_MODELS[provider]
    model = model if model in allowed else default

    # License check
    lm = get_license_manager()
    if not lm.can_grade():
        return jsonify({'error': 'License required. Please activate or start a trial.'}), 403
    if lm.is_trial() and not lm.use_trial_session():
        return jsonify({'error': 'Trial sessions exhausted. Please purchase a license.'}), 403

    # ── Quiz mode batch ──
    if grading_mode == 'quiz':
        prefetch_id = request.form.get('canvas_prefetch_id', '').strip()
        _cleanup_canvas_prefetch()
        prefetch = canvas_prefetch.get(prefetch_id)
        if not prefetch or prefetch.get('grading_mode') != 'quiz':
            return jsonify({'error': 'Quiz session expired. Please fetch submissions again.'}), 400

        quiz_questions = prefetch['questions']
        quiz_submissions = prefetch['submissions']
        answer_key = request.form.get('quiz_answer_key', '').strip() or None

        if not quiz_submissions:
            return jsonify({'error': 'No quiz submissions to grade.'}), 400

        # Build batch requests
        quiz_points_possible = prefetch.get('points_possible')
        system_prompt = build_quiz_system_prompt(quiz_questions, strictness, answer_key=answer_key,
                                                    tone=tone, custom_phrases=custom_phrases,
                                                    points_possible=quiz_points_possible)
        batch_requests = []
        for idx, sub in enumerate(quiz_submissions):
            user_msg = build_quiz_message(sub['filename'], sub['answers'])
            batch_requests.append(GraderAI.prepare_batch_request(idx, system_prompt, user_msg))

        canvas_meta = {
            'canvas_enabled': True,
            'canvas_url': prefetch['canvas_url'],
            'canvas_token': prefetch['canvas_token'],
            'canvas_course_id': prefetch['course_id'],
            'canvas_quiz_id': prefetch['quiz_id'],
            'canvas_points_possible': prefetch.get('points_possible'),
            'quiz_type': prefetch.get('quiz_type', 'classic'),
        }
        essays_data = []
        submissions_data = quiz_submissions

    else:
        # ── Essay mode batch ──
        rubric_text = ''
        rubric_file = request.files.get('rubric_file')
        if rubric_file and rubric_file.filename:
            try:
                rubric_text = extract_text(rubric_file.filename, rubric_file.read())
            except ExtractionError as e:
                return jsonify({'error': f'Could not read rubric: {e}'}), 400
        if not rubric_text:
            rubric_text = request.form.get('rubric_text', '').strip()
        if not rubric_text:
            return jsonify({'error': 'Please provide a rubric.'}), 400

        # Get prefetched essays (Canvas mode) or uploaded files
        prefetch_id = request.form.get('canvas_prefetch_id', '').strip()
        essays_data = []
        canvas_meta = {}

        if prefetch_id:
            _cleanup_canvas_prefetch()
            prefetch = canvas_prefetch.get(prefetch_id)
            if not prefetch:
                return jsonify({'error': 'Canvas data expired. Please fetch submissions again.'}), 400
            essays_data = prefetch.get('submissions', [])
            canvas_meta = {
                'canvas_enabled': True,
                'canvas_url': prefetch['canvas_url'],
                'canvas_token': prefetch['canvas_token'],
                'canvas_course_id': prefetch.get('course_id'),
                'canvas_assignment_id': prefetch.get('assignment_id'),
                'canvas_points_possible': prefetch.get('points_possible'),
            }
        else:
            # Handle file upload (same as /grade route)
            essay_mode = request.form.get('essay_mode', 'files')
            # For batch, just extract essays now
            if essay_mode == 'zip':
                zf = request.files.get('zip_file')
                if not zf:
                    return jsonify({'error': 'No ZIP file provided.'}), 400
                try:
                    with zipfile.ZipFile(BytesIO(zf.read())) as z:
                        for name in z.namelist():
                            ext = os.path.splitext(name)[1].lower()
                            if ext in SUPPORTED_ESSAY_EXTENSIONS and not name.startswith('__MACOSX'):
                                text = extract_text(name, z.read(name))
                                essays_data.append({'filename': os.path.basename(name), 'text': text})
                except Exception as e:
                    return jsonify({'error': f'Error reading ZIP: {e}'}), 400
            else:
                files = request.files.getlist('essay_files')
                for f in files:
                    try:
                        text = extract_text(f.filename, f.read())
                        essays_data.append({'filename': f.filename, 'text': text})
                    except ExtractionError as e:
                        essays_data.append({'filename': f.filename, 'text': '', 'error': str(e)})

        if not essays_data:
            return jsonify({'error': 'No essays to grade.'}), 400

        # Filter out extraction errors
        valid_essays = [e for e in essays_data if not e.get('error') and e.get('text')]
        if not valid_essays:
            return jsonify({'error': 'No readable essays found.'}), 400

        # Build batch requests
        system_prompt = build_system_prompt(rubric_text, strictness,
                                              tone=tone, custom_phrases=custom_phrases)
        batch_requests = []
        for idx, essay in enumerate(valid_essays):
            user_msg = build_essay_message(essay['text'], essay['filename'])
            batch_requests.append(GraderAI.prepare_batch_request(idx, system_prompt, user_msg))

        submissions_data = valid_essays
        quiz_questions = None
        answer_key = None

    # Submit the batch
    try:
        batch_grader = BatchGrader(provider, api_key, model)
        batch_id = batch_grader.submit_batch(batch_requests)
    except BatchError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Failed to submit batch: {e}'}), 500

    # Create session
    session_id = str(uuid.uuid4())
    session['session_id'] = session_id
    strictness_info = get_strictness_info(strictness)
    model_label = MODEL_LABELS.get(model, model)

    sess_data = {
        'status': 'batch_pending',
        'grading_mode': grading_mode,
        'essays': essays_data if grading_mode == 'essay' else [],
        'rubric': rubric_text if grading_mode == 'essay' else '',
        'strictness': strictness,
        'strictness_label': strictness_info['label'],
        'strictness_emoji': strictness_info['emoji'],
        'tone': tone,
        'tone_label': tone_info_batch['label'],
        'tone_emoji': tone_info_batch['emoji'],
        'custom_phrases': custom_phrases or '',
        'model': model,
        'model_label': model_label,
        'results': [],
        'progress': queue.Queue(),
        'total': len(batch_requests),
        'current': 0,
        'batch_id': batch_id,
        'batch_provider': provider,
    }
    if grading_mode == 'quiz':
        sess_data['quiz_questions'] = quiz_questions
        sess_data['quiz_submissions'] = submissions_data
        sess_data['answer_key'] = answer_key
    sess_data.update(canvas_meta)
    sessions[session_id] = sess_data

    # Save batch metadata to disk
    batch_meta = {
        'batch_id': batch_id,
        'session_id': session_id,
        'provider': provider,
        'api_key_hash': batch_store.hash_key(api_key),
        'model': model,
        'status': 'pending',
        'created_at': datetime.now().isoformat(),
        'completed_at': None,
        'total': len(batch_requests),
        'grading_mode': grading_mode,
    }
    batch_store.save_batch(session_id, batch_meta)

    # Save session to disk immediately (persists across app restart)
    _save_session_to_disk(session_id)

    return jsonify({
        'session_id': session_id,
        'batch_id': batch_id,
        'total': len(batch_requests),
        'status': 'batch_pending',
    })


@app.route('/api/batch/poll', methods=['POST'])
def api_batch_poll():
    """Poll batch status and process results when complete."""
    data = request.get_json()
    session_id = (data.get('session_id') or '').strip()
    api_key = (data.get('api_key') or '').strip()

    if not session_id or not api_key:
        return jsonify({'error': 'Missing session_id or api_key.'}), 400

    # Load batch metadata
    batch_meta = batch_store.load_batch(session_id)
    if not batch_meta:
        return jsonify({'error': 'Batch job not found.'}), 404

    # Check if already completed
    if batch_meta.get('status') == 'completed':
        sess = _get_session(session_id)
        if sess and sess.get('results'):
            return jsonify({
                'status': 'completed',
                'total': len(sess['results']),
                'completed_count': len(sess['results']),
            })

    # Verify API key matches
    if batch_store.hash_key(api_key) != batch_meta.get('api_key_hash'):
        return jsonify({'error': 'API key does not match the one used to submit this batch.'}), 403

    provider = batch_meta['provider']
    model = batch_meta['model']
    batch_id = batch_meta['batch_id']

    try:
        grader = BatchGrader(provider, api_key, model)
        poll_result = grader.poll_batch(batch_id)
    except BatchError as e:
        return jsonify({'error': str(e)}), 400

    status = poll_result['status']

    if status == 'completed':
        # Process results
        raw_results = poll_result.get('results', [])
        sess = _get_session(session_id)
        if not sess:
            return jsonify({'error': 'Session not found.'}), 404

        grading_mode = sess.get('grading_mode', 'essay')
        essays = sess.get('essays', [])
        submissions = []

        # For quiz mode, we need the original submission data
        # For essay mode, we map by index

        # Parse each result through _parse_response
        ai = GraderAI(provider, api_key, model=model)
        parsed_results = [None] * batch_meta['total']

        for raw in raw_results:
            idx = int(raw['custom_id'])
            if raw.get('raw_text') is None:
                parsed_results[idx] = {
                    'filename': '',
                    'score': None, 'max_score': None,
                    'summary': None,
                    'error': raw.get('error', 'Batch processing error'),
                }
                continue
            try:
                parsed = ai._parse_response(raw['raw_text'])
                parsed_results[idx] = {
                    'filename': '',
                    'score': parsed['score'],
                    'max_score': parsed['max_score'],
                    'summary': parsed['summary'],
                    'categories': parsed.get('categories', []),
                    'questions': parsed.get('questions', []),
                    'error': None,
                }
            except Exception as e:
                parsed_results[idx] = {
                    'filename': '',
                    'score': None, 'max_score': None,
                    'summary': None,
                    'error': str(e),
                }

        # Enforce known points_possible as max_score for quiz batches
        batch_points_possible = sess.get('canvas_points_possible')
        if grading_mode == 'quiz' and batch_points_possible and batch_points_possible > 0:
            for r in parsed_results:
                if r and r.get('score') is not None:
                    r['max_score'] = float(batch_points_possible)
                    r['score'] = min(r['score'], r['max_score'])

        # Attach filenames and canvas metadata
        if grading_mode == 'essay':
            valid_essays = [e for e in essays if not e.get('error') and e.get('text')]
            for idx, result in enumerate(parsed_results):
                if result and idx < len(valid_essays):
                    result['filename'] = valid_essays[idx].get('filename', f'Essay {idx+1}')
                    if valid_essays[idx].get('canvas_user_id'):
                        result['canvas_user_id'] = valid_essays[idx]['canvas_user_id']
        elif grading_mode == 'quiz':
            quiz_subs = sess.get('quiz_submissions', [])
            for idx, result in enumerate(parsed_results):
                if result and idx < len(quiz_subs):
                    result['filename'] = quiz_subs[idx].get('filename', f'Student {idx+1}')
                    if quiz_subs[idx].get('canvas_user_id'):
                        result['canvas_user_id'] = quiz_subs[idx]['canvas_user_id']

        sess['results'] = [r for r in parsed_results if r is not None]
        sess['status'] = 'complete'

        batch_store.update_batch_status(session_id, 'completed',
                                        completed_at=datetime.now().isoformat())
        _save_session_to_disk(session_id)

        return jsonify({
            'status': 'completed',
            'total': len(sess['results']),
            'completed_count': len(sess['results']),
        })

    elif status == 'failed':
        batch_store.update_batch_status(session_id, 'failed')
        return jsonify({
            'status': 'failed',
            'error': poll_result.get('error', 'Batch processing failed.'),
        })

    # Still in progress
    return jsonify({'status': 'in_progress'})


def _save_session_to_disk(session_id: str):
    """Quick helper to persist current session state to disk."""
    sess = sessions.get(session_id)
    if not sess:
        return
    save_data = {
        'session_id': session_id,
        'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'label': _session_label(sess),
        'grading_mode': sess.get('grading_mode', 'essay'),
        'rubric': sess.get('rubric', ''),
        'strictness': sess.get('strictness', DEFAULT_STRICTNESS),
        'strictness_label': sess.get('strictness_label', ''),
        'strictness_emoji': sess.get('strictness_emoji', ''),
        'tone': sess.get('tone', DEFAULT_TONE),
        'tone_label': sess.get('tone_label', ''),
        'tone_emoji': sess.get('tone_emoji', ''),
        'custom_phrases': sess.get('custom_phrases', ''),
        'model': sess.get('model', ''),
        'model_label': sess.get('model_label', ''),
        'total': sess.get('total', 0),
        'results': sess.get('results', []),
        'essays': sess.get('essays', []),
        'canvas_enabled': sess.get('canvas_enabled', False),
        'canvas_course_id': sess.get('canvas_course_id'),
        'canvas_assignment_id': sess.get('canvas_assignment_id'),
        'canvas_quiz_id': sess.get('canvas_quiz_id'),
        'canvas_points_possible': sess.get('canvas_points_possible'),
        'push_status': sess.get('push_status', {}),
        'status': sess.get('status', 'complete'),
        'batch_id': sess.get('batch_id'),
        'batch_provider': sess.get('batch_provider'),
        # NOTE: canvas_token and canvas_url are intentionally excluded —
        # they are sensitive credentials that must not be persisted to disk.
    }
    if sess.get('grading_mode') == 'quiz':
        save_data['quiz_questions'] = sess.get('quiz_questions', [])
    if sess.get('review'):
        save_data['review'] = sess['review']
    try:
        session_store.save_session(session_id, save_data)
    except Exception:
        pass


# ── AI Consistency Review ────────────────────────────────────────────────────

@app.route('/api/review-scores', methods=['POST'])
def api_review_scores():
    """Run AI consistency review across all graded results."""
    data = request.get_json()
    session_id = (data.get('session_id') or '').strip()
    provider = (data.get('provider') or '').strip().lower()
    api_key = (data.get('api_key') or '').strip()

    if not all([session_id, provider, api_key]):
        return jsonify({'error': 'Missing required fields.'}), 400

    sess = _get_session(session_id)
    if not sess:
        return jsonify({'error': 'Session not found.'}), 404

    if sess.get('status') != 'complete':
        return jsonify({'error': 'Grading must be complete before reviewing.'}), 400

    results = sess.get('results', [])
    valid_results = [r for r in results if not r.get('error')]
    if len(valid_results) < 2:
        return jsonify({'error': 'Need at least 2 graded results to review for consistency.'}), 400

    grading_mode = sess.get('grading_mode', 'essay')
    grading_model = sess.get('model', '')

    # Allow teacher to pick review model; default to same model used for grading
    requested_model = (data.get('review_model') or '').strip()
    if requested_model and requested_model in ALLOWED_MODELS.get(provider, set()):
        review_model = requested_model
    else:
        review_model = get_review_model(provider, grading_model)

    rubric = sess.get('rubric', '')

    essays = sess.get('essays', [])
    system_prompt, user_message = build_review_prompt(rubric, results, grading_mode,
                                                      essays=essays)

    try:
        grader = GraderAI(provider, api_key, model=review_model)
        review = grader.review_scores(system_prompt, user_message)
    except GradingError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Review failed: {e}'}), 500

    # Store review in session
    sess['review'] = review
    _save_session_to_disk(session_id)

    return jsonify({
        'success': True,
        'review_model': review_model,
        'review_model_label': MODEL_LABELS.get(review_model, review_model),
        'review': review,
    })


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
        cal_examples = sess.get('calibration_examples') or None
        graded = grader.grade_essay(rubric, essay['text'], essay['filename'],
                                    new_strictness, calibration_examples=cal_examples)
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


def _kill_stale_instances(port: int):
    """Kill any previous Wolverine Grader Pro processes before starting.

    Handles the case where a user opens a new build while an old one is still
    running — the old server would keep serving stale pages on the same port.
    """
    import signal, re, subprocess

    my_pid = os.getpid()

    # Strategy 1: Kill anything holding our port
    try:
        result = subprocess.run(
            ['lsof', '-ti', f':{port}'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for pid_str in result.stdout.strip().splitlines():
                try:
                    pid = int(pid_str.strip())
                    if pid != my_pid:
                        print(f"  ⚠ Killing stale process on port {port} (PID {pid})")
                        os.kill(pid, signal.SIGTERM)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass

    # Strategy 2: Kill any other WolverineGraderPro processes (catches ones on
    # different ports or stuck in startup)
    try:
        result = subprocess.run(
            ['pgrep', '-f', 'WolverineGraderPro'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for pid_str in result.stdout.strip().splitlines():
                try:
                    pid = int(pid_str.strip())
                    if pid != my_pid:
                        print(f"  ⚠ Killing stale WolverineGraderPro process (PID {pid})")
                        os.kill(pid, signal.SIGTERM)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass

    # Brief pause so the OS releases the port
    time.sleep(0.5)


if __name__ == '__main__':
    import webbrowser, subprocess
    port = int(os.environ.get('PORT', 5050))

    from grader.license_manager import APP_VERSION
    print(f"\n  Wolverine Grader Pro {APP_VERSION}")
    print(f"  Checking for stale instances...")
    _kill_stale_instances(port)

    print(f"  Starting server on http://127.0.0.1:{port}\n")
    # Auto-open the browser after a short delay so the server is ready
    threading.Timer(1.2, lambda: webbrowser.open(f'http://127.0.0.1:{port}')).start()
    app.run(host='127.0.0.1', debug=False, port=port, threaded=True)
