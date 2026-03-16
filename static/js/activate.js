// ─── License Activation Page ──────────────────────────────────────────────────

function showClaimForm() {
    document.getElementById('optionsCard').style.display = 'none';
    document.getElementById('claimCard').style.display = 'block';
    document.getElementById('activateCard').style.display = 'none';
    document.getElementById('orderNumber').focus();
}

function hideClaimForm() {
    document.getElementById('claimCard').style.display = 'none';
    const opts = document.getElementById('optionsCard');
    if (opts) opts.style.display = 'block';
}

function showActivateForm() {
    const opts = document.getElementById('optionsCard');
    if (opts) opts.style.display = 'none';
    document.getElementById('claimCard').style.display = 'none';
    document.getElementById('activateCard').style.display = 'block';
    document.getElementById('licenseKeyInput').focus();
}

function hideActivateForm() {
    document.getElementById('activateCard').style.display = 'none';
    const opts = document.getElementById('optionsCard');
    if (opts) opts.style.display = 'block';
}

// ─── Start Trial ──────────────────────────────────────────────────────────────

async function startTrial() {
    try {
        const resp = await fetch('/api/license/start-trial', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            window.location.href = '/';
        } else {
            alert(data.error || 'Could not start trial.');
        }
    } catch (err) {
        alert('Network error. Please try again.');
    }
}

// ─── Claim TPT Purchase ──────────────────────────────────────────────────────

async function claimPurchase() {
    const orderNumber = document.getElementById('orderNumber').value.trim();
    const buyerName = document.getElementById('buyerName').value.trim();
    const buyerEmail = document.getElementById('buyerEmail').value.trim();
    const errDiv = document.getElementById('claimError');
    const successDiv = document.getElementById('claimSuccess');
    const btn = document.getElementById('claimBtn');

    errDiv.style.display = 'none';
    successDiv.style.display = 'none';

    if (!orderNumber) {
        errDiv.textContent = 'Please enter your TPT order number.';
        errDiv.style.display = 'block';
        return;
    }
    if (!/^\d{9}$/.test(orderNumber)) {
        errDiv.textContent = 'TPT order numbers are 9 digits (e.g. 123456789). Check your TPT receipt email for your order number.';
        errDiv.style.display = 'block';
        return;
    }
    if (!buyerName) {
        errDiv.textContent = 'Please enter the name on your receipt.';
        errDiv.style.display = 'block';
        return;
    }

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Claiming\u2026';

    try {
        const resp = await fetch('/api/license/claim', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order_number: orderNumber, buyer_name: buyerName, buyer_email: buyerEmail }),
        });
        const data = await resp.json();

        if (data.error) {
            errDiv.textContent = data.error;
            errDiv.style.display = 'block';
        } else if (data.success || data.licenseKey) {
            const key = data.licenseKey || data.license_key;
            successDiv.innerHTML = `
                <div class="license-key-display">\u2705 Your License Key</div>
                <div class="license-key-value" style="font-family:monospace; font-size:1.3rem; letter-spacing:2px; font-weight:bold; padding:0.75rem; background:var(--gray-100,#f3f4f6); border-radius:8px; margin:0.75rem 0; user-select:all;">${key}</div>
                <p style="margin-top:0.5rem; font-size:0.85rem; color:var(--gray-600);">
                    ${data.alreadyClaimed ? 'This order was already claimed.' : 'Save this key for your records.'}
                </p>
                <button class="btn btn-sm btn-secondary" style="margin-top:0.75rem;" onclick="navigator.clipboard.writeText('${key}').then(function(){this.innerHTML='\u2705 Copied!';}.bind(this))">
                    \uD83D\uDCCB Copy Key to Clipboard
                </button>
                <button class="btn btn-primary btn-block" style="margin-top:1rem; font-size:1.05rem;" onclick="document.getElementById('licenseKeyInput').value='${key}';activateKey();">
                    Activate &amp; Continue \u2192
                </button>
            `;
            successDiv.style.display = 'block';

            // Pre-fill the key but let the user copy it first
            document.getElementById('licenseKeyInput').value = key;
        }
    } catch (err) {
        errDiv.textContent = 'Network error. Please try again.';
        errDiv.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.innerHTML = 'Claim My License';
    }
}

// ─── Activate License Key ─────────────────────────────────────────────────────

async function activateKey() {
    const licenseKey = document.getElementById('licenseKeyInput').value.trim().toUpperCase();
    const errDiv = document.getElementById('activateError');
    const btn = document.getElementById('activateBtn');

    errDiv.style.display = 'none';

    if (!licenseKey) {
        errDiv.textContent = 'Please enter your license key.';
        errDiv.style.display = 'block';
        return;
    }

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Activating\u2026';

    try {
        const resp = await fetch('/api/license/activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ license_key: licenseKey }),
        });
        const data = await resp.json();

        if (data.error) {
            errDiv.textContent = data.error;
            errDiv.style.display = 'block';

            // If device limit reached, show active devices
            if (data.activeDevices) {
                errDiv.innerHTML += '<br><br><strong>Currently active on:</strong><ul style="margin:0.5rem 0 0 1rem;">' +
                    data.activeDevices.map(d => `<li>${d}</li>`).join('') + '</ul>';
            }
        } else if (data.success) {
            // Activation successful — show key so user can copy it before continuing
            const activateCard = document.getElementById('activateCard');
            activateCard.innerHTML = `
                <div style="text-align:center; padding:0.5rem 0;">
                    <div style="font-size:2.5rem; margin-bottom:0.5rem;">✅</div>
                    <h2 style="margin-bottom:0.25rem;">Activated!</h2>
                    <p style="color:var(--gray-600); margin-bottom:1.25rem;">Save your license key for your records.</p>
                    <div style="font-family:monospace; font-size:1.3rem; letter-spacing:2px; font-weight:bold;
                                padding:0.75rem; background:var(--gray-100,#f3f4f6); border-radius:8px;
                                margin-bottom:0.75rem; user-select:all;">${licenseKey}</div>
                    <button class="btn btn-sm btn-secondary" style="margin-bottom:1.25rem;"
                        onclick="navigator.clipboard.writeText('${licenseKey}').then(() => { this.innerHTML = '✅ Copied!'; })">
                        📋 Copy Key to Clipboard
                    </button>
                    <a href="/?activated=1" class="btn btn-primary btn-block" style="font-size:1.05rem;">
                        Continue to App →
                    </a>
                </div>
            `;
        }
    } catch (err) {
        errDiv.textContent = 'Network error. Please check your internet connection.';
        errDiv.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.innerHTML = 'Activate Device';
    }
}

// ─── Deactivate Device ────────────────────────────────────────────────────────

async function deactivateDevice() {
    if (!confirm('Deactivate this device? You can re-activate later with your license key.')) return;

    try {
        const resp = await fetch('/api/license/deactivate', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            window.location.href = '/activate';
        } else {
            alert(data.error || 'Could not deactivate. Please try again.');
        }
    } catch (err) {
        alert('Network error. Please try again.');
    }
}
