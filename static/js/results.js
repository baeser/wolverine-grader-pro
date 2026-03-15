let results = [];
let currentIndex = 0;
let editedScores = {};
// Per-level feedback edits: { 1: { idx: text }, 2: { idx: text }, 3: { idx: text } }
let feedbackByLevel = { 1: {}, 2: {}, 3: {} };
let currentFeedbackLevel = 1;

// Canvas state
let sessionId       = null;
let canvasEnabled   = false;
let canvasPointsPossible = null;
// Track push status per result index: null | 'pushing' | 'pushed' | {error}
let pushStatus = {};

// Auto-save state
let lastSavedHash   = null;
let pendingSaveTimer = null;
let autoSaveInterval = null;

document.addEventListener('DOMContentLoaded', async function () {
    const params = new URLSearchParams(window.location.search);
    sessionId = params.get('session_id');

    try {
        const resp = await fetch(`/api/results?session_id=${sessionId}`);
        const data = await resp.json();

        if (!resp.ok || !data.results) {
            document.getElementById('loading').innerHTML =
                '<p class="error-msg">Could not load results. <a href="/">Go back</a></p>';
            return;
        }

        results = data.results;
        canvasEnabled = !!data.canvas_enabled;
        canvasPointsPossible = data.canvas_points_possible || null;

        // Restore push status from saved session data (if any)
        if (data.push_status) {
            for (const [k, v] of Object.entries(data.push_status)) {
                pushStatus[k] = v;
            }
        }

        // Restore per-level feedback edits from saved session data (if any)
        results.forEach((r, i) => {
            if (r.edited_feedbacks) {
                [1, 2, 3].forEach(level => {
                    const v = r.edited_feedbacks[level] ?? r.edited_feedbacks[String(level)];
                    if (v !== undefined) feedbackByLevel[level][i] = v;
                });
            }
        });

        // Show strictness + model badge
        if (data.strictness_label) {
            const badge = document.querySelector('.strictness-badge');
            if (badge) {
                const modelPart = data.model_label ? ` · ${data.model_label}` : '';
                const canvasPart = canvasEnabled ? ' · 🎓 Canvas' : '';
                badge.textContent = `${data.strictness_emoji} ${data.strictness_label} (Level ${data.strictness}/10)${modelPart}${canvasPart}`;
            }
        }

        buildDropdown();
        showResult(0);

        // Show "Push All" button for Canvas sessions
        if (canvasEnabled) {
            document.getElementById('pushAllWrap').style.display = 'block';
        }

        document.getElementById('loading').style.display = 'none';
        document.getElementById('resultsContainer').style.display = 'block';

        // Kick off periodic auto-save (every 60 s) and save on page close
        autoSaveInterval = setInterval(autoSave, 60_000);
        window.addEventListener('beforeunload', () => autoSave());
    } catch (err) {
        document.getElementById('loading').innerHTML =
            '<p class="error-msg">Network error loading results. <a href="/">Go back</a></p>';
    }
});

function buildDropdown() {
    const select = document.getElementById('essaySelect');
    const prevValue = select.value;
    select.innerHTML = '';
    results.forEach((r, i) => {
        const opt = document.createElement('option');
        opt.value = i;
        const pushed = pushStatus[i] === 'pushed';
        const indicator = pushed ? ' ✅' : '';
        opt.textContent = `${i + 1}. ${r.filename}${r.error ? ' (error)' : ''}${indicator}`;
        select.appendChild(opt);
    });
    select.value = prevValue || 0;
}

/** Rebuild the dropdown to reflect updated push status indicators. */
function refreshDropdown() {
    buildDropdown();
}

function showResult(idx) {
    saveCurrent();

    currentIndex = idx;
    const r = results[idx];

    document.getElementById('essaySelect').value = idx;
    document.getElementById('counterText').textContent = `${idx + 1} of ${results.length}`;
    document.getElementById('prevBtn').disabled = idx === 0;
    document.getElementById('nextBtn').disabled = idx === results.length - 1;

    if (r.error) {
        document.getElementById('resultCard').style.display = 'none';
        document.getElementById('errorCard').style.display = 'block';
        document.getElementById('errorFilename').textContent = r.filename;
        document.getElementById('errorMsg').textContent = `Error: ${r.error}`;
    } else {
        document.getElementById('errorCard').style.display = 'none';
        document.getElementById('resultCard').style.display = 'block';
        document.getElementById('filename').textContent = r.filename;

        // Render editable score input (use saved edit if available)
        const displayScore = editedScores[idx] !== undefined ? editedScores[idx] : r.score;
        const maxScore = r.max_score || 100;
        let scoreHTML = `<input type="number" class="score-input" id="scoreInput"
                            value="${displayScore}" min="0" max="${maxScore}" step="0.5"
                            oninput="onScoreInput()" title="Click to edit score">` +
                        ` <span class="max">/ ${maxScore}</span>` +
                        `<div class="score-edit-hint">✏️ click score to edit</div>`;

        // Show Canvas-scaled score if applicable
        if (canvasEnabled && canvasPointsPossible && maxScore && maxScore !== canvasPointsPossible) {
            const scaled = ((displayScore / maxScore) * canvasPointsPossible).toFixed(2);
            scoreHTML += `<div id="canvasScaledScore" style="font-size:0.85rem;color:var(--gray-600);font-weight:400;margin-top:0.2rem;">` +
                         `→ <strong>${scaled} / ${canvasPointsPossible}</strong> in Canvas</div>`;
        }
        document.getElementById('scoreDisplay').innerHTML = scoreHTML;

        document.getElementById('feedbackArea').value = getCurrentFeedback(idx);
    }

    // Render Push to Canvas button
    renderPushButton(idx);

    // Dim level 2/3 toggle if this essay has no categories
    updateToggleState(idx);

    // Auto-save after a short pause (debounced so rapid navigation batches)
    clearTimeout(pendingSaveTimer);
    pendingSaveTimer = setTimeout(autoSave, 1500);
}

function saveCurrent() {
    if (results[currentIndex] && !results[currentIndex].error) {
        feedbackByLevel[currentFeedbackLevel][currentIndex] =
            document.getElementById('feedbackArea').value;
        const scoreInput = document.getElementById('scoreInput');
        if (scoreInput) {
            const val = parseFloat(scoreInput.value);
            if (!isNaN(val)) editedScores[currentIndex] = val;
        }
    }
}

// Returns the teacher-adjusted score for idx, falling back to AI score
function getCurrentScore(idx) {
    return editedScores[idx] !== undefined ? editedScores[idx] : results[idx].score;
}

// Called live as the teacher types a new score — updates Canvas scaled display
function onScoreInput() {
    const input = document.getElementById('scoreInput');
    const score = parseFloat(input.value);
    if (isNaN(score)) return;
    const r = results[currentIndex];
    const scaledDiv = document.getElementById('canvasScaledScore');
    if (scaledDiv && canvasPointsPossible && r.max_score) {
        const scaled = ((score / r.max_score) * canvasPointsPossible).toFixed(2);
        scaledDiv.innerHTML = `→ <strong>${scaled} / ${canvasPointsPossible}</strong> in Canvas`;
    }
}

// ─── Feedback detail levels ───────────────────────────────────────────────────

/**
 * Build the formatted feedback string for a given result at a given detail level.
 * Level 1: summary only
 * Level 2: category scores + summary
 * Level 3: full category breakdown with explanations + summary
 */
function buildFeedbackText(idx, level) {
    const r = results[idx];
    if (!r || r.error) return '';
    const summary = r.summary || '';
    const cats = Array.isArray(r.categories) ? r.categories : [];

    if (level === 1 || cats.length === 0) return summary;

    const lines = cats.map(c => {
        const score = `${c.earned}/${c.possible}`;
        if (level === 2) {
            return `\u2022 ${c.name}: ${score}`;
        } else {
            const note = c.explanation ? ` \u2014 ${c.explanation}` : '';
            return `\u2022 ${c.name}: ${score}${note}`;
        }
    });

    const header = level === 2 ? 'Category Scores:' : 'Category Breakdown:';
    return `${header}\n${lines.join('\n')}\n\n${summary}`;
}

/** Return the feedback text for idx at the current level (teacher edit or AI-generated). */
function getCurrentFeedback(idx) {
    const override = feedbackByLevel[currentFeedbackLevel][idx];
    return override !== undefined ? override : buildFeedbackText(idx, currentFeedbackLevel);
}

/** Called when the teacher clicks a different detail level toggle button. */
function setFeedbackLevel(level) {
    saveCurrent();  // persist whatever was in the textarea at the previous level
    currentFeedbackLevel = level;

    // Update the textarea for the current student
    if (results[currentIndex] && !results[currentIndex].error) {
        document.getElementById('feedbackArea').value = getCurrentFeedback(currentIndex);
    }

    // Grey out levels 2 & 3 if this student has no category data
    updateToggleState(currentIndex);
}

/** Dim levels 2 and 3 toggle labels when the current essay has no category data. */
function updateToggleState(idx) {
    const r = results[idx];
    const hasCats = r && !r.error && Array.isArray(r.categories) && r.categories.length > 0;
    document.querySelectorAll('.detail-opt').forEach(opt => {
        const val = parseInt(opt.querySelector('input').value);
        opt.classList.toggle('detail-opt-disabled', val > 1 && !hasCats);
        opt.title = (val > 1 && !hasCats)
            ? 'Category breakdown not available (rubric had no distinct categories)'
            : '';
    });
}

// ─── Auto-save ────────────────────────────────────────────────────────────────
async function autoSave() {
    if (!sessionId || results.length === 0) return;
    saveCurrent(); // capture whatever is currently in the UI

    // Build a clean copy of push_status (only 'pushed' states matter for persistence)
    const persistedPushStatus = {};
    for (const [k, v] of Object.entries(pushStatus)) {
        if (v === 'pushed') persistedPushStatus[k] = 'pushed';
    }

    const payload = {
        session_id: sessionId,
        edited_scores: editedScores,
        edited_feedbacks: {
            '1': feedbackByLevel[1],
            '2': feedbackByLevel[2],
            '3': feedbackByLevel[3],
        },
        push_status: persistedPushStatus,
    };

    // Skip the network call if nothing has changed since last save
    const stateHash = JSON.stringify(payload);
    if (stateHash === lastSavedHash) return;

    try {
        const resp = await fetch('/api/session/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (resp.ok) {
            lastSavedHash = stateHash;
            showAutoSaveBadge();
        }
    } catch (e) { /* silent — never interrupt the teacher's workflow */ }
}

function showAutoSaveBadge() {
    const badge = document.getElementById('autoSaveBadge');
    if (!badge) return;
    badge.classList.add('show');
    clearTimeout(badge._timer);
    badge._timer = setTimeout(() => badge.classList.remove('show'), 3000);
}

function navigate(dir) {
    const newIdx = currentIndex + dir;
    if (newIdx >= 0 && newIdx < results.length) {
        showResult(newIdx);
    }
}

function goTo(idx) {
    showResult(parseInt(idx, 10));
}

// ─── Copy helpers ─────────────────────────────────────────────────────────────
function copyToCanvas() {
    saveCurrent();
    const r = results[currentIndex];
    const feedback = getCurrentFeedback(currentIndex);
    const score = getCurrentScore(currentIndex);
    const text = `Score: ${score}/${r.max_score}\n\nFeedback: ${feedback}`;
    copyText(text);
}

function copyScoreOnly() {
    copyText(`${getCurrentScore(currentIndex)}`);
}

function copyText(text) {
    navigator.clipboard.writeText(text).then(() => {
        const toast = document.getElementById('copyToast');
        toast.classList.add('show');
        setTimeout(() => toast.classList.remove('show'), 2000);
    });
}

// ─── Push to Canvas ───────────────────────────────────────────────────────────
function renderPushButton(idx) {
    const wrap = document.getElementById('canvasPushWrap');
    if (!canvasEnabled) {
        wrap.style.display = 'none';
        return;
    }

    const r = results[idx];
    if (r.error) {
        wrap.style.display = 'none';
        return;
    }

    wrap.style.display = 'inline-flex';
    const status = pushStatus[idx];

    if (status === 'pushing') {
        wrap.innerHTML = `<button class="btn-push-canvas" disabled>
            <span class="spinner"></span> Pushing&hellip;
        </button>`;
    } else if (status === 'pushed') {
        wrap.innerHTML = `<span class="canvas-push-status success">✅ Pushed to Canvas</span>`;
    } else if (status && status.error) {
        wrap.innerHTML = `<span class="canvas-push-status error" title="Click to retry" onclick="pushToCanvas(${idx})">
            ❌ ${status.error} — retry?
        </span>`;
    } else {
        wrap.innerHTML = `<button class="btn-push-canvas" onclick="pushToCanvas(${idx})">
            🎓 Push to Canvas
        </button>`;
    }
}

async function pushToCanvas(idx) {
    saveCurrent();
    const feedback = getCurrentFeedback(idx);

    pushStatus[idx] = 'pushing';
    renderPushButton(idx);

    try {
        const resp = await fetch('/api/canvas/push-grade', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, idx, feedback, score: getCurrentScore(idx) }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            pushStatus[idx] = { error: data.error || 'Push failed' };
        } else {
            pushStatus[idx] = 'pushed';
            // Show a brief toast
            const toast = document.getElementById('copyToast');
            toast.textContent = `✅ Grade pushed to Canvas!`;
            toast.classList.add('show');
            setTimeout(() => {
                toast.classList.remove('show');
                toast.textContent = 'Copied to clipboard!';
            }, 2500);
        }
    } catch (err) {
        pushStatus[idx] = { error: 'Network error' };
    }

    renderPushButton(idx);
    refreshDropdown();
}

// ─── Re-Grade ─────────────────────────────────────────────────────────
async function regradeEssay() {
    const idx = currentIndex;
    const newStrictness = parseInt(document.getElementById('regradeStrictness').value);
    const btn = document.getElementById('regradeBtn');

    // Pull provider/key from localStorage (same keys the index page uses)
    const provider = localStorage.getItem('wgp_provider') || '';
    const apiKey = localStorage.getItem('wgp_api_key') || '';
    const model = localStorage.getItem('wgp_model') || '';

    if (!provider || !apiKey) {
        alert('Cannot re-grade: API provider and key not found. Please go back to the main page and enter your API key with "Remember" checked.');
        return;
    }

    // Disable button and show spinner
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Re-Grading\u2026';

    try {
        const resp = await fetch('/api/regrade', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: sessionId,
                idx: idx,
                strictness: newStrictness,
                provider: provider,
                api_key: apiKey,
                model: model,
            }),
        });

        const data = await resp.json();

        if (!resp.ok) {
            alert('Re-grade failed: ' + (data.error || 'Unknown error'));
            return;
        }

        // Update the result in our local array
        results[idx] = data.result;

        // Clear any teacher edits for this student (fresh result)
        delete editedScores[idx];
        [1, 2, 3].forEach(level => delete feedbackByLevel[level][idx]);

        // Re-render the current student
        showResult(idx);

        // Show success toast
        const toast = document.getElementById('copyToast');
        toast.textContent = `\u2705 Re-graded at ${data.strictness_emoji} ${data.strictness_label}`;
        toast.classList.add('show');
        setTimeout(() => {
            toast.classList.remove('show');
            toast.textContent = 'Copied to clipboard!';
        }, 3000);

    } catch (err) {
        alert('Network error during re-grade. Please try again.');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '\uD83D\uDD04 Re-Grade';
    }
}

// ─── Show Essay Modal ─────────────────────────────────────────────────────
async function showEssay() {
    const modal = document.getElementById('essayModal');
    const title = document.getElementById('essayModalTitle');
    const content = document.getElementById('essayModalContent');
    const r = results[currentIndex];

    title.textContent = r.filename || 'Student Essay';
    content.textContent = 'Loading essay...';
    modal.classList.add('open');

    try {
        const resp = await fetch(`/api/essay-text/${currentIndex}?session_id=${sessionId}`);
        const data = await resp.json();

        if (!resp.ok) {
            content.textContent = data.error || 'Could not load essay text.';
        } else {
            content.textContent = data.text;
        }
    } catch (err) {
        content.textContent = 'Network error loading essay.';
    }
}

// ─── Push All to Canvas ───────────────────────────────────────────────────────
function confirmPushAll() {
    saveCurrent();
    // Count how many valid (non-error) results we have
    const validCount = results.filter(r => !r.error).length;
    document.getElementById('pushAllCount').textContent = `${validCount}`;
    document.getElementById('pushAllModal').classList.add('open');
}

async function executePushAll() {
    document.getElementById('pushAllModal').classList.remove('open');

    const btn = document.getElementById('pushAllBtn');
    const progressDiv = document.getElementById('pushAllProgress');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Pushing\u2026';
    progressDiv.style.display = 'block';

    let pushed = 0;
    let failed = 0;
    const total = results.filter(r => !r.error).length;

    progressDiv.innerHTML = `<div class="push-all-progress-text">Pushing 0 / ${total}\u2026</div>`;

    for (let i = 0; i < results.length; i++) {
        const r = results[i];
        if (r.error) continue;

        // Skip already-pushed
        if (pushStatus[i] === 'pushed') {
            pushed++;
            progressDiv.innerHTML = `<div class="push-all-progress-text">Pushing ${pushed + failed} / ${total}\u2026 (${pushed} \u2705 ${failed > 0 ? failed + ' \u274c' : ''})</div>`;
            continue;
        }

        const feedback = getCurrentFeedback(i);
        const score = getCurrentScore(i);

        try {
            const resp = await fetch('/api/canvas/push-grade', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, idx: i, feedback, score }),
            });
            const data = await resp.json();

            if (resp.ok) {
                pushStatus[i] = 'pushed';
                pushed++;
            } else {
                pushStatus[i] = { error: data.error || 'Push failed' };
                failed++;
            }
        } catch (err) {
            pushStatus[i] = { error: 'Network error' };
            failed++;
        }

        progressDiv.innerHTML = `<div class="push-all-progress-text">Pushing ${pushed + failed} / ${total}\u2026 (${pushed} \u2705${failed > 0 ? ' ' + failed + ' \u274c' : ''})</div>`;
    }

    // Done
    const resultEmoji = failed === 0 ? '\u2705' : '\u26a0\ufe0f';
    const resultMsg = failed === 0
        ? `${resultEmoji} All ${pushed} grades pushed to Canvas!`
        : `${resultEmoji} ${pushed} pushed, ${failed} failed.`;

    progressDiv.innerHTML = `<div class="push-all-progress-text done">${resultMsg}</div>`;
    btn.disabled = false;
    btn.innerHTML = failed === 0
        ? '\u2705 All Pushed!'
        : '\u2622\ufe0f Retry Failed';

    // Re-render current student's push button and dropdown indicators
    renderPushButton(currentIndex);
    refreshDropdown();

    // Toast
    const toast = document.getElementById('copyToast');
    toast.textContent = resultMsg;
    toast.classList.add('show');
    setTimeout(() => {
        toast.classList.remove('show');
        toast.textContent = 'Copied to clipboard!';
    }, 4000);
}

// ─── Export ───────────────────────────────────────────────────────────────────
function exportCSV() {
    saveCurrent();
    let csv = 'Filename,Score,Max Score,Feedback\n';
    results.forEach((r, i) => {
        if (r.error) {
            csv += `"${r.filename}","ERROR","","${r.error}"\n`;
        } else {
            const score = editedScores[i] !== undefined ? editedScores[i] : r.score;
            const feedback = getCurrentFeedback(i).replace(/"/g, '""');
            csv += `"${r.filename}","${score}","${r.max_score}","${feedback}"\n`;
        }
    });
    downloadFile('grades.csv', csv, 'text/csv');
}

function exportJSON() {
    saveCurrent();
    const data = results.map((r, i) => ({
        filename: r.filename,
        score: editedScores[i] !== undefined ? editedScores[i] : r.score,
        max_score: r.max_score,
        feedback: getCurrentFeedback(i) || null,
        categories: r.categories || [],
        error: r.error || null,
    }));
    downloadFile('grades.json', JSON.stringify(data, null, 2), 'application/json');
}

// ─── Report ──────────────────────────────────────────────────────────────────
function openReport() {
    window.open(`/report?session_id=${sessionId}`, '_blank');
}

// ─── Add Essays to Session ───────────────────────────────────────────────────
let addEssayFiles = [];

function toggleAddEssays() {
    const panel = document.getElementById('addEssaysPanel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

function handleAddFiles(fileList) {
    const valid = ['.docx', '.pdf', '.txt'];
    for (const f of fileList) {
        const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
        if (valid.includes(ext) && !addEssayFiles.some(x => x.name === f.name)) {
            addEssayFiles.push(f);
        }
    }
    renderAddFileList();
}

function removeAddFile(idx) {
    addEssayFiles.splice(idx, 1);
    renderAddFileList();
}

function renderAddFileList() {
    const listDiv = document.getElementById('addEssayFileList');
    const btn = document.getElementById('addEssaysBtn');
    if (addEssayFiles.length === 0) {
        listDiv.style.display = 'none';
        btn.style.display = 'none';
        return;
    }
    listDiv.style.display = 'block';
    btn.style.display = 'inline-flex';
    listDiv.innerHTML = addEssayFiles.map((f, i) =>
        `<div class="essay-file-item">
            <span class="file-icon">\uD83D\uDCC4</span>
            <span class="file-name">${f.name}</span>
            <button class="remove-file" onclick="removeAddFile(${i})">&times;</button>
        </div>`
    ).join('');
}

async function submitAddEssays() {
    if (addEssayFiles.length === 0) return;

    const provider = localStorage.getItem('wgp_provider') || '';
    const apiKey = localStorage.getItem('wgp_api_key') || '';

    if (!provider || !apiKey) {
        alert('Cannot add essays: API provider and key not found. Please go back to the main page and enter your API key with "Remember" checked.');
        return;
    }

    const btn = document.getElementById('addEssaysBtn');
    const errDiv = document.getElementById('addEssaysError');
    errDiv.style.display = 'none';
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Grading\u2026';

    const formData = new FormData();
    formData.append('session_id', sessionId);
    formData.append('provider', provider);
    formData.append('api_key', apiKey);
    addEssayFiles.forEach(f => formData.append('essay_files', f, f.name));

    try {
        const resp = await fetch('/api/session/add-essays', { method: 'POST', body: formData });
        const data = await resp.json();

        if (!resp.ok) {
            errDiv.textContent = data.error || 'Failed to add essays.';
            errDiv.style.display = 'block';
        } else {
            // Reload the page to show updated results
            const toast = document.getElementById('copyToast');
            toast.textContent = `\u2705 Added ${data.added} essay(s) — now ${data.new_total} total`;
            toast.classList.add('show');
            setTimeout(() => {
                toast.classList.remove('show');
                toast.textContent = 'Copied to clipboard!';
            }, 3000);
            // Refresh results by reloading
            window.location.reload();
        }
    } catch (err) {
        errDiv.textContent = 'Network error. Please try again.';
        errDiv.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.innerHTML = '\uD83D\uDCDA Grade & Add to Batch';
    }
}

// Set up drag-and-drop for add essays
document.addEventListener('DOMContentLoaded', () => {
    const zone = document.getElementById('addDropZone');
    if (!zone) return;
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
    zone.addEventListener('drop', e => {
        e.preventDefault();
        zone.classList.remove('drag-over');
        handleAddFiles(e.dataTransfer.files);
    });
});

function downloadFile(name, content, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
}
