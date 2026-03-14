document.addEventListener('DOMContentLoaded', function () {
    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get('session_id');
    const total = parseInt(params.get('total'), 10) || 0;

    const progressText = document.getElementById('progressText');
    const progressBar = document.getElementById('progressBar');
    const logList = document.getElementById('logList');
    const fatalError = document.getElementById('fatalError');

    if (!sessionId) {
        fatalError.textContent = 'No grading session found. Please go back and try again.';
        fatalError.style.display = 'block';
        return;
    }

    progressText.textContent = `Grading 0 of ${total} essays...`;

    const evtSource = new EventSource(`/grade/status?session_id=${sessionId}`);

    evtSource.onmessage = function (event) {
        const data = JSON.parse(event.data);

        if (data.type === 'heartbeat') return;

        if (data.type === 'error' && data.fatal) {
            evtSource.close();
            fatalError.textContent = data.message;
            fatalError.style.display = 'block';
            progressText.textContent = 'Grading failed.';
            return;
        }

        if (data.type === 'progress') {
            const pct = Math.round((data.current / data.total) * 100);
            progressBar.style.width = pct + '%';
            progressBar.textContent = pct + '%';
            progressText.textContent = `Grading ${data.current} of ${data.total} essays...`;

            const li = document.createElement('li');
            li.className = 'log-item ' + (data.status === 'error' ? 'error' : 'success');
            li.innerHTML = `<span class="log-icon">${data.status === 'error' ? 'X' : '+'}</span> ${data.filename} ${data.status === 'error' ? '(error)' : '(done)'}`;
            logList.appendChild(li);
            logList.scrollTop = logList.scrollHeight;
        }

        if (data.type === 'complete') {
            evtSource.close();
            progressText.textContent = 'Grading complete! Redirecting to results...';
            progressBar.style.width = '100%';
            progressBar.textContent = '100%';
            setTimeout(() => {
                window.location.href = `/results?session_id=${sessionId}`;
            }, 1000);
        }
    };

    evtSource.onerror = function () {
        evtSource.close();
        fatalError.textContent = 'Lost connection to server. Please refresh the page.';
        fatalError.style.display = 'block';
    };
});
