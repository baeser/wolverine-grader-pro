// ─── Strictness slider ────────────────────────────────────────────────────────
const STRICTNESS_DATA = {
    2:  { label: "Effort Counts!",                  emoji: "🌟" },
    4:  { label: "Standard Classroom Mode",          emoji: "📚" },
    6:  { label: "Red Pen Activated",                emoji: "🖊️" },
    8:  { label: "Grammar Detective",                emoji: "🔍" },
    10: { label: "College Professor Who Hates You",  emoji: "💀" },
};

function updateStrictness(val) {
    val = parseInt(val, 10);
    const info = STRICTNESS_DATA[val];
    if (!info) return;
    document.getElementById('strictnessEmoji').textContent = info.emoji;
    document.getElementById('strictnessName').textContent  = info.label;
    document.getElementById('strictnessLevel').textContent = `Level ${val}/10`;
    document.getElementById('strictnessDisplay').className = `strictness-label-display level-${val}`;
    document.querySelectorAll('.strictness-ticks span').forEach(el => {
        el.classList.toggle('active', parseInt(el.dataset.val, 10) === val);
    });
}

// ─── Provider / model switching ───────────────────────────────────────────────
const ALL_PROVIDERS = ['claude', 'openai', 'gemini'];

function switchProvider(provider) {
    ALL_PROVIDERS.forEach(p => {
        const isActive = p === provider;
        document.getElementById(`card-${p}`).classList.toggle('active', isActive);
        document.getElementById(`models-${p}`).style.display    = isActive ? 'block' : 'none';
        document.getElementById(`model-${p}`).disabled          = !isActive;
        document.getElementById(`model-hint-${p}`).style.display = isActive ? 'block' : 'none';
    });
}

// ─── Tab switching (Paste / Upload / Build) ───────────────────────────────────
function switchTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    const btn = document.querySelector(`.tab-btn[onclick="switchTab('${tab}')"]`);
    if (btn) btn.classList.add('active');
    const content = document.getElementById(`tab-${tab}`);
    if (content) content.classList.add('active');
}

// ─── Essay upload tab switching ───────────────────────────────────────────────
function switchEssayTab(tab) {
    ['zip', 'files', 'canvas'].forEach(t => {
        document.getElementById(`essay-tab-${t}`).classList.toggle('active', t === tab);
        document.getElementById(`essay-content-${t}`).classList.toggle('active', t === tab);
    });
    document.getElementById('essay_mode').value = tab;
}

// ─── API key visibility toggle ────────────────────────────────────────────────
function toggleKey() {
    const input = document.getElementById('api_key');
    const btn   = document.querySelector('.key-toggle');
    if (input.type === 'password') { input.type = 'text';     btn.textContent = 'Hide'; }
    else                           { input.type = 'password'; btn.textContent = 'Show'; }
}

// ─── API key localStorage persistence ────────────────────────────────────────
const LS_KEY_PREFIX = 'wgp3_apikey_'; // prefix per provider

function getSavedKey(provider) {
    return localStorage.getItem(LS_KEY_PREFIX + provider) || '';
}

function loadSavedKey() {
    const provider = document.querySelector('input[name="provider"]:checked')?.value || 'claude';
    const saved = getSavedKey(provider);
    if (saved) {
        document.getElementById('api_key').value = saved;
        document.getElementById('rememberKey').checked = true;
        document.getElementById('forgetBtn').style.display = 'inline-flex';
    }
}

function saveKeyIfChecked() {
    const provider = document.querySelector('input[name="provider"]:checked')?.value || 'claude';
    const key = document.getElementById('api_key').value.trim();
    if (document.getElementById('rememberKey').checked && key) {
        localStorage.setItem(LS_KEY_PREFIX + provider, key);
    } else {
        localStorage.removeItem(LS_KEY_PREFIX + provider);
    }
}

function forgetKey() {
    const provider = document.querySelector('input[name="provider"]:checked')?.value || 'claude';
    localStorage.removeItem(LS_KEY_PREFIX + provider);
    document.getElementById('api_key').value = '';
    document.getElementById('rememberKey').checked = false;
    document.getElementById('forgetBtn').style.display = 'none';
}

// When provider changes, reload the saved key for that provider
document.querySelectorAll('input[name="provider"]').forEach(radio => {
    radio.addEventListener('change', () => {
        document.getElementById('api_key').value = '';
        document.getElementById('rememberKey').checked = false;
        document.getElementById('forgetBtn').style.display = 'none';
        loadSavedKey();
    });
});

// Show/hide forget button when checkbox changes
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('rememberKey').addEventListener('change', function () {
        const provider = document.querySelector('input[name="provider"]:checked')?.value || 'claude';
        if (!this.checked) {
            localStorage.removeItem(LS_KEY_PREFIX + provider);
            document.getElementById('forgetBtn').style.display = 'none';
        }
    });
});

// ─── Rubric builder ───────────────────────────────────────────────────────────
async function generateRubric() {
    const description = document.getElementById('rubricPrompt').value.trim();
    if (!description) {
        showBuildError('Please describe the rubric you want to generate.');
        return;
    }

    const provider = document.querySelector('input[name="provider"]:checked')?.value;
    const apiKey   = document.getElementById('api_key').value.trim();
    const modelEl  = document.getElementById(`model-${provider}`);
    const model    = modelEl ? modelEl.value : '';

    if (!apiKey) {
        showBuildError('Please enter your API key above before generating a rubric.');
        return;
    }

    const btn = document.getElementById('generateBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Generating...';
    document.getElementById('buildError').style.display  = 'none';
    document.getElementById('buildOutput').style.display = 'none';

    try {
        const resp = await fetch('/api/generate-rubric', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, api_key: apiKey, model, description }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            showBuildError(data.error || 'Generation failed. Please try again.');
            return;
        }

        document.getElementById('generatedRubric').value  = data.rubric;
        document.getElementById('buildOutput').style.display = 'block';
    } catch (err) {
        showBuildError('Network error. Please try again.');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '✨ Generate Rubric';
    }
}

function showBuildError(msg) {
    const el = document.getElementById('buildError');
    el.textContent = msg;
    el.style.display = 'block';
}

function useGeneratedRubric() {
    const rubric = document.getElementById('generatedRubric').value.trim();
    if (!rubric) return;
    // Copy into the paste textarea and switch to that tab
    document.getElementById('rubric_text').value = rubric;
    switchTab('paste');
}

// ─── ZIP file info ────────────────────────────────────────────────────────────
document.getElementById('zip_file').addEventListener('change', function () {
    const info = document.getElementById('file-info');
    if (this.files.length > 0) {
        const file  = this.files[0];
        const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
        info.textContent = `Selected: ${file.name} (${sizeMB} MB)`;
        info.style.display = 'block';
    } else {
        info.style.display = 'none';
    }
});

// ─── Select Files: drop zone + file list ──────────────────────────────────────
let selectedEssayFiles = [];
const VALID_ESSAY_EXTS = ['.docx', '.pdf', '.txt'];

function addEssayFiles(fileList) {
    Array.from(fileList).forEach(file => {
        const ext = file.name.substring(file.name.lastIndexOf('.')).toLowerCase();
        if (!VALID_ESSAY_EXTS.includes(ext)) return;
        // Deduplicate by name
        if (selectedEssayFiles.some(f => f.name === file.name)) return;
        selectedEssayFiles.push(file);
    });
    renderEssayFileList();
}

function removeEssayFile(index) {
    selectedEssayFiles.splice(index, 1);
    renderEssayFileList();
}

function clearEssayFiles() {
    selectedEssayFiles = [];
    renderEssayFileList();
}

function renderEssayFileList() {
    const container = document.getElementById('essay-file-list');
    if (selectedEssayFiles.length === 0) {
        container.style.display = 'none';
        return;
    }

    const getIcon = name => {
        const ext = name.substring(name.lastIndexOf('.')).toLowerCase();
        if (ext === '.pdf')  return '📄';
        if (ext === '.docx') return '📝';
        return '📃';
    };

    const fmtSize = bytes => {
        if (bytes < 1024)        return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    };

    container.style.display = 'block';
    container.innerHTML = `
        <div class="essay-file-list-header">
            <span>${selectedEssayFiles.length} file${selectedEssayFiles.length !== 1 ? 's' : ''} selected</span>
            <button type="button"
                onclick="clearEssayFiles()"
                style="background:none;border:none;color:var(--red);font-size:0.8rem;cursor:pointer;font-weight:600;">
                Clear all
            </button>
        </div>
        ${selectedEssayFiles.map((file, i) => `
            <div class="essay-file-item">
                <span class="file-icon">${getIcon(file.name)}</span>
                <span class="file-name">${file.name}</span>
                <span class="file-size">${fmtSize(file.size)}</span>
                <button type="button" class="remove-file" onclick="removeEssayFile(${i})" title="Remove">&#215;</button>
            </div>
        `).join('')}
    `;
}

// ─── Calibration toggle ──────────────────────────────────────────────────────
function toggleCalibration() {
    const content = document.getElementById('calibrationContent');
    const enabled = document.getElementById('enableCalibration').checked;
    content.style.display = enabled ? 'block' : 'none';
}

// ─── Form submission ──────────────────────────────────────────────────────────
document.getElementById('gradeForm').addEventListener('submit', async function (e) {
    e.preventDefault();

    saveKeyIfChecked();

    const errorDiv = document.getElementById('form-error');
    errorDiv.style.display = 'none';

    const essayMode = document.getElementById('essay_mode').value;

    // Validate essay input for whichever mode is active
    if (essayMode === 'zip') {
        const zipFile = document.getElementById('zip_file').files[0];
        if (!zipFile) {
            errorDiv.textContent = 'Please upload a ZIP file of essays.';
            errorDiv.style.display = 'block';
            return;
        }
    } else if (essayMode === 'files') {
        if (selectedEssayFiles.length === 0) {
            errorDiv.textContent = 'Please select at least one essay file (.docx, .pdf, or .txt).';
            errorDiv.style.display = 'block';
            return;
        }
    } else if (essayMode === 'canvas') {
        const prefetchId = document.getElementById('canvas_prefetch_id').value;
        if (!prefetchId) {
            errorDiv.textContent = 'Please fetch Canvas submissions before grading.';
            errorDiv.style.display = 'block';
            return;
        }
    }

    const btn = document.getElementById('submitBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Uploading...';

    const formData = new FormData(this);

    // For files mode, manually append each selected file
    if (essayMode === 'files') {
        selectedEssayFiles.forEach(file => {
            formData.append('essay_files', file, file.name);
        });
    }

    try {
        const resp = await fetch('/grade', { method: 'POST', body: formData });
        const data = await resp.json();

        if (!resp.ok) {
            errorDiv.textContent = data.error || 'Something went wrong.';
            errorDiv.style.display = 'block';
            btn.disabled = false;
            btn.textContent = 'Start Grading';
            return;
        }

        // Save provider, model, and API key to localStorage for re-grading on results page
        const provider = document.querySelector('input[name="provider"]:checked')?.value || '';
        const model = document.querySelector('select[name="model"]:not(:disabled)')?.value || '';
        const apiKey = document.getElementById('api_key').value.trim();
        localStorage.setItem('wgp_provider', provider);
        localStorage.setItem('wgp_model', model);
        if (apiKey) localStorage.setItem('wgp_api_key', apiKey);

        window.location.href = `/processing?session_id=${data.session_id}&total=${data.total}`;
    } catch (err) {
        errorDiv.textContent = 'Network error. Please try again.';
        errorDiv.style.display = 'block';
        btn.disabled = false;
        btn.textContent = 'Start Grading';
    }
});

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    // Strictness ticks clickable
    document.querySelectorAll('.strictness-ticks span').forEach(el => {
        el.addEventListener('click', function () {
            document.getElementById('strictness').value = this.dataset.val;
            updateStrictness(this.dataset.val);
        });
    });
    updateStrictness(document.getElementById('strictness').value);

    // Load any saved key
    loadSavedKey();

    // ── Drop zone setup ───────────────────────────────────────────────────────
    const dropZone       = document.getElementById('dropZone');
    const essayFileInput = document.getElementById('essay_files');

    // Click anywhere on drop zone to open file picker
    dropZone.addEventListener('click', () => essayFileInput.click());

    // Drag visual feedback
    dropZone.addEventListener('dragover', e => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragleave', e => {
        // Only remove highlight if actually leaving the zone (not hovering a child element)
        if (!dropZone.contains(e.relatedTarget)) {
            dropZone.classList.remove('drag-over');
        }
    });

    dropZone.addEventListener('drop', e => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        addEssayFiles(e.dataTransfer.files);
    });

    // File picker selection
    essayFileInput.addEventListener('change', function () {
        addEssayFiles(this.files);
        this.value = ''; // reset so same file can be re-added after removal
    });
});
