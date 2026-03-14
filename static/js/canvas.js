// ─── Canvas LMS Integration ───────────────────────────────────────────────────
// localStorage keys
const LS_CANVAS_URL   = 'wgp3_canvas_url';
const LS_CANVAS_TOKEN = 'wgp3_canvas_token';

// State
let canvasConnected  = false;
let canvasUrl        = '';
let canvasToken      = '';
let canvasCourseId   = null;
let canvasAssignmentId = null;
let canvasPointsPossible = null;

// ─── Token visibility ────────────────────────────────────────────────────────
function toggleCanvasToken() {
    const input = document.getElementById('canvas_token');
    const btn   = input.parentElement.querySelector('.key-toggle');
    if (input.type === 'password') { input.type = 'text';     btn.textContent = 'Hide'; }
    else                           { input.type = 'password'; btn.textContent = 'Show'; }
}

// ─── localStorage helpers ────────────────────────────────────────────────────
function loadCanvasCredentials() {
    const savedUrl   = localStorage.getItem(LS_CANVAS_URL)   || '';
    const savedToken = localStorage.getItem(LS_CANVAS_TOKEN) || '';
    if (savedUrl || savedToken) {
        document.getElementById('canvas_url').value          = savedUrl;
        document.getElementById('canvas_token').value        = savedToken;
        document.getElementById('rememberCanvasKey').checked = true;
        document.getElementById('forgetCanvasBtn').style.display = 'inline-flex';
    }
}

function saveCanvasCredentials() {
    if (document.getElementById('rememberCanvasKey').checked) {
        localStorage.setItem(LS_CANVAS_URL,   document.getElementById('canvas_url').value.trim());
        localStorage.setItem(LS_CANVAS_TOKEN, document.getElementById('canvas_token').value.trim());
    }
}

function forgetCanvasKey() {
    localStorage.removeItem(LS_CANVAS_URL);
    localStorage.removeItem(LS_CANVAS_TOKEN);
    document.getElementById('canvas_url').value          = '';
    document.getElementById('canvas_token').value        = '';
    document.getElementById('rememberCanvasKey').checked = false;
    document.getElementById('forgetCanvasBtn').style.display = 'none';
    // Reset connection state
    resetCanvasConnection();
}

function resetCanvasConnection() {
    canvasConnected = false;
    document.getElementById('canvas-selectors').style.display = 'none';
    document.getElementById('canvas-connect-error').style.display = 'none';
    document.getElementById('canvas-preview').style.display = 'none';
    document.getElementById('canvas_prefetch_id').value = '';
}

// ─── Connect to Canvas ───────────────────────────────────────────────────────
async function connectCanvas() {
    const url   = document.getElementById('canvas_url').value.trim();
    const token = document.getElementById('canvas_token').value.trim();
    const errEl = document.getElementById('canvas-connect-error');
    errEl.style.display = 'none';

    if (!url)   { showCanvasConnectError('Please enter your Canvas URL.');   return; }
    if (!token) { showCanvasConnectError('Please enter your Canvas API token.'); return; }

    const btn = document.getElementById('connectCanvasBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Connecting&hellip;';

    try {
        const resp = await fetch(
            `/api/canvas/courses?canvas_url=${encodeURIComponent(url)}&canvas_token=${encodeURIComponent(token)}`
        );
        const data = await resp.json();

        if (!resp.ok) {
            showCanvasConnectError(data.error || 'Connection failed.');
            return;
        }

        // Save credentials if checkbox is checked
        canvasUrl   = url;
        canvasToken = token;
        saveCanvasCredentials();

        // Populate course dropdown
        const courseSelect = document.getElementById('canvas_course');
        courseSelect.innerHTML = '<option value="">Select a course&hellip;</option>';
        (data.courses || []).forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.code ? `${c.name} (${c.code})` : c.name;
            courseSelect.appendChild(opt);
        });

        if (!data.courses || data.courses.length === 0) {
            showCanvasConnectError('No active teacher courses found for this account.');
            return;
        }

        // Show connected state
        canvasConnected = true;
        const badge = document.getElementById('canvas-connected-badge');
        const domain = url.replace(/^https?:\/\//, '').replace(/\/.*$/, '');
        badge.textContent = `✓ Connected to ${domain}`;
        document.getElementById('canvas-selectors').style.display = 'block';
        document.getElementById('canvas-assignment-group').style.display = 'none';
        document.getElementById('canvas-fetch-wrap').style.display = 'none';
        document.getElementById('canvas-preview').style.display = 'none';
        document.getElementById('canvas_prefetch_id').value = '';

    } catch (err) {
        showCanvasConnectError('Network error. Please check the Canvas URL and try again.');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '🔌 Connect to Canvas';
    }
}

function showCanvasConnectError(msg) {
    const el = document.getElementById('canvas-connect-error');
    el.textContent = msg;
    el.style.display = 'block';
}

// ─── Load assignments for selected course ────────────────────────────────────
async function loadCanvasAssignments() {
    const courseId = document.getElementById('canvas_course').value;
    canvasCourseId = courseId || null;

    const assignGroup = document.getElementById('canvas-assignment-group');
    const fetchWrap   = document.getElementById('canvas-fetch-wrap');
    const fetchErr    = document.getElementById('canvas-fetch-error');
    const preview     = document.getElementById('canvas-preview');

    assignGroup.style.display = 'none';
    fetchWrap.style.display   = 'none';
    fetchErr.style.display    = 'none';
    preview.style.display     = 'none';
    document.getElementById('canvas_prefetch_id').value = '';

    if (!courseId) return;

    const assignSelect = document.getElementById('canvas_assignment');
    assignSelect.innerHTML = '<option value="">Loading&hellip;</option>';
    assignSelect.disabled = true;
    assignGroup.style.display = 'block';

    try {
        const resp = await fetch(
            `/api/canvas/assignments?canvas_url=${encodeURIComponent(canvasUrl)}&canvas_token=${encodeURIComponent(canvasToken)}&course_id=${courseId}`
        );
        const data = await resp.json();

        assignSelect.innerHTML = '<option value="">Select an assignment&hellip;</option>';
        (data.assignments || []).forEach(a => {
            const opt = document.createElement('option');
            opt.value = a.id;
            opt.dataset.points = a.points_possible || '';
            opt.textContent = a.points_possible != null
                ? `${a.name} (${a.points_possible} pts)`
                : a.name;
            assignSelect.appendChild(opt);
        });
        assignSelect.disabled = false;

        if (!data.assignments || data.assignments.length === 0) {
            assignSelect.innerHTML = '<option value="">No assignments found</option>';
        }
    } catch (err) {
        assignSelect.innerHTML = '<option value="">Error loading assignments</option>';
        assignSelect.disabled = false;
    }
}

function onCanvasAssignmentChange() {
    const select = document.getElementById('canvas_assignment');
    const assignId = select.value;
    canvasAssignmentId = assignId || null;

    const fetchWrap = document.getElementById('canvas-fetch-wrap');
    const preview   = document.getElementById('canvas-preview');
    const fetchErr  = document.getElementById('canvas-fetch-error');

    fetchErr.style.display = 'none';
    preview.style.display  = 'none';
    document.getElementById('canvas_prefetch_id').value = '';

    if (assignId) {
        const opt = select.options[select.selectedIndex];
        canvasPointsPossible = opt.dataset.points ? parseFloat(opt.dataset.points) : null;
        fetchWrap.style.display = 'block';
    } else {
        fetchWrap.style.display = 'none';
        canvasPointsPossible = null;
    }
}

// ─── Fetch submissions (SSE-based with live progress) ────────────────────────
async function fetchCanvasSubmissions() {
    const fetchErr = document.getElementById('canvas-fetch-error');
    const preview  = document.getElementById('canvas-preview');
    fetchErr.style.display = 'none';
    preview.style.display  = 'none';
    document.getElementById('canvas_prefetch_id').value = '';

    const btn = document.getElementById('fetchSubmissionsBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Starting&hellip;';

    // Step 1: start background fetch task
    let fetchId;
    try {
        const resp = await fetch('/api/canvas/start-fetch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                canvas_url:      canvasUrl,
                canvas_token:    canvasToken,
                course_id:       parseInt(canvasCourseId),
                assignment_id:   parseInt(canvasAssignmentId),
                points_possible: canvasPointsPossible,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            showCanvasFetchError(data.error || 'Failed to start fetch.');
            btn.disabled = false;
            btn.innerHTML = '⬇️ Fetch Submissions';
            return;
        }
        fetchId = data.fetch_id;
    } catch (err) {
        showCanvasFetchError('Network error. Please try again.');
        btn.disabled = false;
        btn.innerHTML = '⬇️ Fetch Submissions';
        return;
    }

    // Step 2: open SSE stream and render live progress
    preview.style.display = 'block';
    preview.innerHTML = buildFetchProgressHTML(0, 0);
    btn.innerHTML = '<span class="spinner"></span> Fetching&hellip;';

    const es = new EventSource(`/api/canvas/fetch-progress/${fetchId}`);
    let total = 0;

    es.onmessage = function (e) {
        const msg = JSON.parse(e.data);
        if (msg.type === 'heartbeat') return;

        if (msg.type === 'start') {
            total = msg.total;
            preview.innerHTML = buildFetchProgressHTML(0, total);

        } else if (msg.type === 'progress') {
            total = msg.total;
            updateFetchBar(msg.current, total);
            addFetchLogItem(msg.name, msg.status, msg.reason || null);

        } else if (msg.type === 'complete') {
            es.close();
            btn.disabled = false;
            btn.innerHTML = '⬇️ Fetch Submissions';
            document.getElementById('canvas_prefetch_id').value = msg.prefetch_id;
            renderCanvasPreview(msg);

        } else if (msg.type === 'error') {
            es.close();
            btn.disabled = false;
            btn.innerHTML = '⬇️ Fetch Submissions';
            preview.style.display = 'none';
            showCanvasFetchError(msg.message || 'Failed to fetch submissions.');
        }
    };

    es.onerror = function () {
        es.close();
        btn.disabled = false;
        btn.innerHTML = '⬇️ Fetch Submissions';
        preview.style.display = 'none';
        showCanvasFetchError('Connection lost while fetching. Please try again.');
    };
}

function showCanvasFetchError(msg) {
    const el = document.getElementById('canvas-fetch-error');
    el.textContent = msg;
    el.style.display = 'block';
}

function buildFetchProgressHTML(current, total) {
    const pct = total > 0 ? Math.round((current / total) * 100) : 0;
    const countLabel = total > 0 ? `${current} / ${total}` : 'connecting\u2026';
    return `
        <div class="canvas-fetch-progress">
            <div class="cfp-header">
                <span class="cfp-status-text">Fetching submissions&hellip;</span>
                <span class="cfp-counter">${countLabel}</span>
            </div>
            <div class="cfp-bar-track">
                <div class="cfp-bar-fill" id="cfpBarFill" style="width:${pct}%"></div>
            </div>
            <div class="cfp-log" id="cfpLog"></div>
        </div>`;
}

function updateFetchBar(current, total) {
    const bar     = document.getElementById('cfpBarFill');
    const counter = document.querySelector('.cfp-counter');
    if (bar && total > 0) bar.style.width = `${Math.round((current / total) * 100)}%`;
    if (counter) counter.textContent = `${current} / ${total}`;
}

function addFetchLogItem(name, status, reason) {
    const log = document.getElementById('cfpLog');
    if (!log) return;
    const icon = status === 'ready' ? '✓' : '⏭';
    const cls  = status === 'ready' ? 'cfp-log-item ready' : 'cfp-log-item skipped';
    const note = reason ? ` <span class="cfp-log-note">${reason}</span>` : '';
    const div  = document.createElement('div');
    div.className = cls;
    div.innerHTML = `<span class="cfp-log-icon">${icon}</span><span class="cfp-log-name">${name}</span>${note}`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
}

function renderCanvasPreview(data) {
    const preview = document.getElementById('canvas-preview');
    const skippedNote = data.skipped > 0
        ? `<span style="color:var(--gray-600); font-weight:400;">&nbsp;&bull;&nbsp;${data.skipped} not yet submitted</span>`
        : '';
    const pointsNote = data.points_possible != null
        ? `<span style="color:var(--gray-600); font-weight:400;">&nbsp;&bull;&nbsp;${data.points_possible} pts</span>`
        : '';

    const items = (data.preview || []).map(p => {
        if (p.status === 'skipped') {
            return `<div class="canvas-preview-item skipped">
                        <span>⏭</span>
                        <span class="cpi-name">${p.name}</span>
                        <span class="cpi-file">${p.reason || 'No submission'}</span>
                    </div>`;
        }
        const fileLabel = p.file && p.file !== 'text_entry'
            ? `<span class="cpi-file">${p.file}</span>`
            : `<span class="cpi-file" style="font-style:italic;">text entry</span>`;
        return `<div class="canvas-preview-item">
                    <span>✓</span>
                    <span class="cpi-name">${p.name}</span>
                    ${fileLabel}
                </div>`;
    }).join('');

    preview.style.display = 'block';
    preview.innerHTML = `
        <div class="canvas-preview-box">
            <div class="canvas-preview-header">
                <span>✅ ${data.count} submission${data.count !== 1 ? 's' : ''} ready to grade${skippedNote}${pointsNote}</span>
            </div>
            <div class="canvas-preview-list">${items}</div>
        </div>
        <p class="help-text" style="margin-top:0.4rem;">
            ✏️ All AI-generated grades will be reviewed before anything is pushed to Canvas.
        </p>
    `;
}

// ─── Init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    loadCanvasCredentials();

    // Show/hide forget button when remember checkbox changes
    const rememberCb = document.getElementById('rememberCanvasKey');
    if (rememberCb) {
        rememberCb.addEventListener('change', function () {
            if (!this.checked) {
                localStorage.removeItem(LS_CANVAS_URL);
                localStorage.removeItem(LS_CANVAS_TOKEN);
                document.getElementById('forgetCanvasBtn').style.display = 'none';
            } else {
                document.getElementById('forgetCanvasBtn').style.display = 'inline-flex';
            }
        });
    }
});
