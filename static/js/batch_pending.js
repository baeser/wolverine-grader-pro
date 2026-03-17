// ─── Batch Pending — Poll for results ────────────────────────────────────────

(function () {
    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get('session_id');
    const total = parseInt(params.get('total') || '0', 10);

    const statusEl = document.getElementById('batchStatus');
    const totalEl = document.getElementById('batchTotal');
    const progressBar = document.getElementById('batchProgressBar');
    const errorEl = document.getElementById('batchError');

    if (totalEl) totalEl.textContent = total;

    if (!sessionId) {
        statusEl.textContent = 'No session found.';
        return;
    }

    const apiKey = localStorage.getItem('wgp_api_key') || '';

    if (!apiKey) {
        statusEl.textContent = 'API key not found. Please go back and re-enter your key.';
        statusEl.style.color = '#b71c1c';
        return;
    }

    let pollCount = 0;
    const POLL_INTERVAL = 30000; // 30 seconds
    const MAX_POLLS = 2880;      // 24 hours max

    async function poll() {
        pollCount++;

        try {
            const resp = await fetch('/api/batch/poll', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: sessionId,
                    api_key: apiKey,
                }),
            });
            const data = await resp.json();

            if (!resp.ok) {
                errorEl.textContent = data.error || 'Error checking batch status.';
                errorEl.style.display = 'block';
                statusEl.textContent = 'Error';
                statusEl.style.color = '#b71c1c';
                return; // Stop polling on error
            }

            if (data.status === 'completed') {
                statusEl.textContent = '✅ Grading complete!';
                statusEl.style.color = '#2e7d32';
                progressBar.style.width = '100%';
                progressBar.style.background = '#2e7d32';

                // Auto-redirect to results after 1 second
                setTimeout(() => {
                    window.location.href = `/results?session_id=${sessionId}`;
                }, 1000);
                return; // Stop polling
            }

            if (data.status === 'failed') {
                errorEl.textContent = data.error || 'Batch processing failed.';
                errorEl.style.display = 'block';
                statusEl.textContent = 'Failed';
                statusEl.style.color = '#b71c1c';
                return; // Stop polling
            }

            // Still in progress
            statusEl.textContent = `Processing... (checked ${pollCount} time${pollCount > 1 ? 's' : ''})`;

            // Animate progress bar slowly
            const progress = Math.min(90, pollCount * 5);
            progressBar.style.width = `${progress}%`;

        } catch (err) {
            // Network error — keep polling
            statusEl.textContent = `Waiting for results... (retry ${pollCount})`;
        }

        // Schedule next poll
        if (pollCount < MAX_POLLS) {
            setTimeout(poll, POLL_INTERVAL);
        } else {
            statusEl.textContent = 'Timed out. Please try loading this session later.';
        }
    }

    // Start polling immediately, then every 30 seconds
    poll();
})();
