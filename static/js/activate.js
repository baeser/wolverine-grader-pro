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
                <div class="license-key-value">${key}</div>
                <p style="margin-top:0.5rem; font-size:0.85rem; color:var(--gray-600);">
                    ${data.alreadyClaimed ? 'This order was already claimed.' : 'Save this key for your records.'}
                </p>
            `;
            successDiv.style.display = 'block';

            // Auto-activate with the new key
            document.getElementById('licenseKeyInput').value = key;
            setTimeout(() => activateKey(), 500);
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
            // Activation successful — redirect to main app
            window.location.href = '/?activated=1';
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
