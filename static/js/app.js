document.addEventListener('DOMContentLoaded', () => {
    const $ = (id) => document.getElementById(id);
    const VIEW_TITLES = { resumen: 'Resumen', cola: 'Cola', historial: 'Historial', equipos: 'Equipos' };
    const STATUS_LABELS = {
        printed: ['Impreso', 'ok'], denied: ['Denegado', 'bad'], canceled: ['Cancelado', 'muted'],
        error: ['Error', 'bad'], active: ['En proceso', 'warn']
    };
    const NUM = new Intl.NumberFormat('es-CL');

    const reviewToggle = $('review-mode-toggle');
    let currentView = 'resumen';
    let currentActive = null;
    let modalResolver = null;
    let previewJob = null;
    let previewCurrentPage = 1;
    let lastReport = null;
    let searchTimer = null;
    let knownHeld = null;
    let heldCount = 0;
    let blinkOn = false;
    let audioContext = null;
    let desktopNotice = null;
    const BASE_TITLE = document.title;
    const iconLink = document.querySelector('link[rel="icon"]');
    const BASE_ICON = { href: iconLink.href, type: iconLink.type };

    // --- NAVEGACIÓN ---

    function showView() {
        const name = location.hash.slice(1);
        currentView = VIEW_TITLES[name] ? name : 'resumen';
        document.querySelectorAll('.view').forEach(v => { v.hidden = v.dataset.view !== currentView; });
        document.querySelectorAll('.nav-link').forEach(link => {
            const active = link.dataset.view === currentView;
            link.classList.toggle('active', active);
            if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
        });
        $('view-title').textContent = VIEW_TITLES[currentView];
        refreshView();
    }

    function refreshView() {
        if (currentView === 'resumen') fetchReport();
        if (currentView === 'historial') fetchHistory();
        if (currentView === 'equipos') fetchUsers();
    }

    async function getJson(url) {
        const response = await fetch(url);
        if (response.status === 401) { window.location.href = '/login'; throw new Error('auth'); }
        if (!response.ok) throw new Error(`Error ${response.status} en ${url}`);
        return response.json();
    }

    // --- ESTADO Y COLA ---

    async function fetchStatus() {
        try {
            const data = await getJson('/api/status');
            const held = data.active.filter(job => job.held).length;

            $('printer-status-text').textContent = data.status_text;
            $('printer-status-text').className = `status-indicator ${data.status_class}`;
            reviewToggle.checked = data.review_mode;
            $('stat-pending').textContent = NUM.format(held);
            $('active-badge').textContent = `${data.active.length} en cola`;
            $('nav-pending').textContent = held;
            $('nav-pending').hidden = held === 0;
            updateAlerts(data.active.filter(job => job.held));
            renderAlerts(data.alerts);

            const serialized = JSON.stringify(data.active);
            if (serialized !== currentActive) {
                currentActive = serialized;
                renderActiveJobs(data.active);
            }
        } catch (error) {
            console.error('Error actualizando el panel:', error);
        }
    }

    // --- ALERTAS DE IMPRESIONES POR AUTORIZAR ---

    function alertsOn() {
        try { return localStorage.getItem('webcups-alerts') === 'on'; } catch (e) { return false; }
    }

    function canNotify() {
        return window.isSecureContext && 'Notification' in window;
    }

    function updateAlerts(heldJobs) {
        const newJobs = knownHeld ? heldJobs.filter(job => !knownHeld.has(job.id)) : [];
        const changed = heldJobs.length !== heldCount;
        knownHeld = new Set(heldJobs.map(job => job.id));
        heldCount = heldJobs.length;

        if (changed) drawTabIcon();
        paintTitle();

        if (heldCount === 0 && desktopNotice) {
            desktopNotice.close();
            desktopNotice = null;
        }
        if (newJobs.length === 0) return;

        showToast(newJobs.length === 1 ? '1 impresión nueva por autorizar.' : `${newJobs.length} impresiones nuevas por autorizar.`);
        if (alertsOn()) {
            playChime();
            showDesktopNotice(newJobs);
        }
    }

    function paintTitle() {
        if (heldCount === 0) {
            document.title = BASE_TITLE;
        } else if (document.hidden && blinkOn) {
            document.title = heldCount === 1 ? '¡1 por autorizar!' : `¡${heldCount} por autorizar!`;
        } else {
            document.title = `(${heldCount}) ${BASE_TITLE}`;
        }
    }

    function drawTabIcon() {
        if (heldCount === 0) {
            iconLink.type = BASE_ICON.type;
            iconLink.href = BASE_ICON.href;
            return;
        }

        const count = heldCount;
        const image = new Image();
        image.onload = () => {
            if (count !== heldCount) return;
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = 64;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(image, 0, 0, 64, 64);
            ctx.beginPath();
            ctx.arc(44, 20, 20, 0, Math.PI * 2);
            ctx.fillStyle = '#DC2626';
            ctx.fill();
            ctx.fillStyle = '#FFFFFF';
            ctx.font = 'bold 28px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(count > 9 ? '9+' : String(count), 44, 22);
            iconLink.type = 'image/png';
            iconLink.href = canvas.toDataURL('image/png');
        };
        image.src = BASE_ICON.href;
    }

    function playChime() {
        if (!audioContext || audioContext.state !== 'running') return;
        [880, 1175].forEach((frequency, index) => {
            const start = audioContext.currentTime + index * 0.18;
            const oscillator = audioContext.createOscillator();
            const gain = audioContext.createGain();
            oscillator.frequency.value = frequency;
            gain.gain.setValueAtTime(0.15, start);
            gain.gain.exponentialRampToValueAtTime(0.001, start + 0.16);
            oscillator.connect(gain).connect(audioContext.destination);
            oscillator.start(start);
            oscillator.stop(start + 0.16);
        });
    }

    function showDesktopNotice(newJobs) {
        if (!canNotify() || Notification.permission !== 'granted') return;
        const job = newJobs[0];
        const sheets = job.sheets ? ` · ${job.sheets} ${job.sheets === 1 ? 'hoja' : 'hojas'}` : '';
        const body = newJobs.length === 1
            ? `${job.user}: ${job.name || 'Documento'}${sheets}`
            : `${newJobs.length} trabajos nuevos. Total en espera: ${heldCount}.`;

        if (desktopNotice) desktopNotice.close();
        desktopNotice = new Notification('Impresiones por autorizar', {
            body, tag: 'webcups-pending', renotify: true, requireInteraction: true, icon: BASE_ICON.href
        });
        desktopNotice.onclick = () => {
            window.focus();
            location.hash = 'cola';
            desktopNotice.close();
        };
    }

    function unlockAudio() {
        const Context = window.AudioContext || window.webkitAudioContext;
        if (!Context) return;
        if (!audioContext) audioContext = new Context();
        if (audioContext.state === 'suspended') audioContext.resume();
    }

    function paintAlertsToggle() {
        const on = alertsOn();
        const label = on ? 'Desactivar alertas de impresiones por autorizar' : 'Activar alertas de impresiones por autorizar';
        const button = $('alerts-toggle');
        button.setAttribute('aria-pressed', String(on));
        button.title = label;
        button.setAttribute('aria-label', label);
    }

    $('alerts-toggle').addEventListener('click', async () => {
        const enable = !alertsOn();
        try { localStorage.setItem('webcups-alerts', enable ? 'on' : 'off'); } catch (e) {}
        paintAlertsToggle();
        if (!enable) {
            showToast('Alertas desactivadas.');
            return;
        }

        unlockAudio();
        setTimeout(playChime, 150);

        if (!canNotify()) {
            showToast('Sonido activado. Para avisos de escritorio, Chrome debe tratar este sitio como seguro.', true, 9000);
            return;
        }
        const permission = await Notification.requestPermission();
        showToast(permission === 'granted'
            ? 'Alertas activadas.'
            : 'Sonido activado. El navegador bloqueó los avisos de escritorio.', permission !== 'granted', 7000);
    });

    // El navegador solo permite sonido después de una interacción con la página
    document.addEventListener('pointerdown', () => { if (alertsOn()) unlockAudio(); }, { capture: true });
    document.addEventListener('visibilitychange', paintTitle);
    setInterval(() => {
        blinkOn = !blinkOn;
        if (heldCount > 0) paintTitle();
    }, 1000);
    paintAlertsToggle();

    function renderAlerts(count) {
        $('alert-banner').hidden = !count;
        $('alert-text').textContent = count === 1
            ? '1 trabajo se imprimió sin pasar por Autorizar.'
            : `${count} trabajos se imprimieron sin pasar por Autorizar.`;
    }

    function renderActiveJobs(jobs) {
        const tbody = $('active-jobs-tbody');
        if (jobs.length === 0) {
            tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No hay trabajos en la cola.</td></tr>';
            return;
        }

        tbody.innerHTML = jobs.map(job => `
            <tr class="${job.held ? 'row-held' : ''}">
                <td>${renderThumb(job.id, job.preview, job.pages, job.name)}</td>
                <td class="font-mono">${escapeHtml(job.id)}</td>
                <td>${renderUser(job.user, job.account, job.name)}</td>
                <td><span class="printer-tag">${escapeHtml(job.printer)}</span></td>
                <td>${renderSheets(job)}</td>
                <td class="text-right">
                    ${job.held ? `
                        <div class="action-group">
                            <button class="btn btn-success btn-sm action-approve" data-job-id="${escapeHtml(job.id)}" data-user="${escapeHtml(job.user)}">Autorizar</button>
                            <button class="btn btn-danger btn-sm action-deny" data-job-id="${escapeHtml(job.id)}" data-user="${escapeHtml(job.user)}">Denegar</button>
                        </div>
                    ` : `<button class="btn btn-danger btn-sm action-cancel" data-job-id="${escapeHtml(job.id)}">Cancelar</button>`}
                </td>
            </tr>
        `).join('');
    }

    function renderUser(user, account, name) {
        const who = account ? `${escapeHtml(user)} <span class="text-muted">· ${escapeHtml(account)}</span>` : escapeHtml(user || '—');
        const doc = name ? `<div class="job-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div>` : '';
        return `<div>${who}</div>${doc}`;
    }

    function renderThumb(id, available, pages, name) {
        if (!available) return '<div class="thumb thumb-empty" title="Sin vista previa"></div>';
        return `
            <button class="thumb action-preview" data-job-id="${escapeHtml(id)}" data-pages="${escapeHtml(pages || 1)}"
                    data-name="${escapeHtml(name || '')}" title="Ver documento">
                <img src="/preview/${encodeURIComponent(id)}.png" alt="Página 1 del trabajo ${escapeHtml(id)}" loading="lazy">
            </button>`;
    }

    function renderSheets(job) {
        if (!job.sheets) return '<span class="text-muted">—</span>';
        const details = [`${job.pages} pág.`];
        if (job.copies > 1) details.push(`${job.copies} copias`);
        if (job.duplex) details.push('doble cara');
        return `<div class="sheets-count">${NUM.format(job.sheets)}</div><div class="sheets-detail">${details.join(' · ')}</div>`;
    }

    // --- RESUMEN ---

    async function fetchReport() {
        try {
            lastReport = await getJson(`/api/report?days=${$('report-days').value}`);
            const { totals, by_day: byDay, users } = lastReport;
            $('stat-sheets').textContent = NUM.format(totals.sheets);
            $('stat-printed').textContent = NUM.format(totals.printed);
            $('stat-saved').textContent = NUM.format(totals.saved);

            $('report-note').hidden = !totals.unknown;
            $('report-note').textContent = totals.unknown
                ? `${NUM.format(totals.unknown)} trabajos impresos no tienen hojas registradas (anteriores al registro o sin vista previa) y no suman en las hojas.`
                : '';

            drawDayChart(byDay);
            $('chart-table-body').innerHTML = byDay.map(row =>
                `<tr><td>${escapeHtml(formatDay(row.day))}</td><td class="text-right">${NUM.format(row.sheets)}</td></tr>`).join('');
            renderTopUsers(users.filter(user => user.sheets > 0).slice(0, 5));
        } catch (error) {
            console.error('Error cargando el resumen:', error);
        }
    }

    function formatDay(day) {
        const [y, m, d] = day.split('-').map(Number);
        return new Date(y, m - 1, d).toLocaleDateString('es-CL', { day: 'numeric', month: 'short' });
    }

    function niceMax(value) {
        if (value <= 4) return 4;
        const power = 10 ** Math.floor(Math.log10(value));
        const step = [1, 2, 2.5, 5, 10].map(f => f * power).find(f => f * 4 >= value);
        return step * 4;
    }

    function svgEl(tag, attrs) {
        const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
        for (const key in attrs) el.setAttribute(key, attrs[key]);
        return el;
    }

    function drawDayChart(rows) {
        const box = $('chart-days');
        box.textContent = '';
        const total = rows.reduce((sum, row) => sum + row.sheets, 0);
        if (!total) {
            const empty = document.createElement('p');
            empty.className = 'chart-empty';
            empty.textContent = 'Sin hojas registradas en este período.';
            box.appendChild(empty);
            return;
        }

        const width = Math.max(box.clientWidth, 280);
        const height = 220;
        const margin = { top: 12, right: 4, bottom: 24, left: 36 };
        const plotW = width - margin.left - margin.right;
        const plotH = height - margin.top - margin.bottom;
        const max = niceMax(Math.max(...rows.map(row => row.sheets)));
        const band = plotW / rows.length;
        const barW = Math.max(Math.min(24, band - 2), 2);
        const svg = svgEl('svg', { width, height, viewBox: `0 0 ${width} ${height}`, role: 'img',
            'aria-label': `Hojas impresas por día, total ${total}` });

        for (let i = 0; i <= 4; i++) {
            const y = margin.top + plotH - (plotH * i) / 4;
            svg.appendChild(svgEl('line', { x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: 'chart-grid' }));
            const label = svgEl('text', { x: margin.left - 8, y: y + 4, 'text-anchor': 'end', class: 'chart-axis' });
            label.textContent = NUM.format((max * i) / 4);
            svg.appendChild(label);
        }

        const labelEvery = Math.ceil(rows.length / Math.max(Math.floor(plotW / 56), 1));
        rows.forEach((row, i) => {
            const x = margin.left + band * i + (band - barW) / 2;
            const barH = (row.sheets / max) * plotH;
            const baseY = margin.top + plotH;

            if (row.sheets > 0) {
                const r = Math.min(4, barH, barW / 2);
                svg.appendChild(svgEl('path', { class: 'chart-bar', d:
                    `M${x},${baseY}V${baseY - barH + r}Q${x},${baseY - barH} ${x + r},${baseY - barH}` +
                    `H${x + barW - r}Q${x + barW},${baseY - barH} ${x + barW},${baseY - barH + r}V${baseY}Z` }));
            }

            if ((rows.length - 1 - i) % labelEvery === 0) {
                const label = svgEl('text', { x: x + barW / 2, y: height - 6, 'text-anchor': 'middle', class: 'chart-axis' });
                label.textContent = formatDay(row.day);
                svg.appendChild(label);
            }

            // Zona de interacción más grande que la barra
            const hit = svgEl('rect', { x: margin.left + band * i, y: margin.top, width: band, height: plotH,
                class: 'chart-hit', tabindex: 0, role: 'img', 'aria-label': `${formatDay(row.day)}: ${row.sheets} hojas` });
            const show = (event) => showTooltip(event, hit, row);
            hit.addEventListener('pointermove', show);
            hit.addEventListener('focus', show);
            hit.addEventListener('pointerleave', hideTooltip);
            hit.addEventListener('blur', hideTooltip);
            svg.appendChild(hit);
        });
        box.appendChild(svg);
    }

    function showTooltip(event, hit, row) {
        const tooltip = $('chart-tooltip');
        tooltip.textContent = '';
        const value = document.createElement('strong');
        value.textContent = `${NUM.format(row.sheets)} ${row.sheets === 1 ? 'hoja' : 'hojas'}`;
        const day = document.createElement('span');
        day.textContent = formatDay(row.day);
        tooltip.append(value, day);
        tooltip.hidden = false;

        const rect = hit.getBoundingClientRect();
        const x = event.clientX || rect.left + rect.width / 2;
        const y = event.clientY || rect.top + rect.height / 2;
        const maxX = window.innerWidth - tooltip.offsetWidth - 8;
        tooltip.style.left = `${Math.max(8, Math.min(x + 12, maxX))}px`;
        tooltip.style.top = `${Math.max(8, y - tooltip.offsetHeight - 10)}px`;
    }

    function hideTooltip() {
        $('chart-tooltip').hidden = true;
    }

    function renderTopUsers(users) {
        const box = $('top-users');
        if (users.length === 0) {
            box.innerHTML = '<p class="chart-empty">Sin datos en este período.</p>';
            return;
        }
        const max = users[0].sheets;
        box.innerHTML = users.map(user => `
            <div class="bar-row">
                <div class="bar-label"><span>${escapeHtml(user.user)}</span><strong>${NUM.format(user.sheets)}</strong></div>
                <div class="bar-track"><div class="bar-fill" style="width:${Math.max((user.sheets / max) * 100, 2)}%"></div></div>
            </div>`).join('');
    }

    // --- HISTORIAL ---

    async function fetchHistory() {
        const params = new URLSearchParams({ q: $('history-q').value.trim(), status: $('history-status').value, days: $('history-days').value });
        try {
            renderHistory((await getJson(`/api/history?${params}`)).jobs);
        } catch (error) {
            console.error('Error cargando el historial:', error);
        }
    }

    function renderHistory(jobs) {
        const tbody = $('history-tbody');
        if (jobs.length === 0) {
            tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No hay trabajos con estos filtros.</td></tr>';
            return;
        }
        tbody.innerHTML = jobs.map(job => {
            const [label, tone] = STATUS_LABELS[job.status] || STATUS_LABELS.active;
            return `
            <tr>
                <td class="font-mono">${escapeHtml(job.id)}</td>
                <td class="text-muted nowrap">${escapeHtml(formatDate(job.created_at))}</td>
                <td>${renderUser(job.user, job.account)}</td>
                <td><div class="job-name" title="${escapeHtml(job.name || '')}">${escapeHtml(job.name || '—')}</div></td>
                <td>${job.sheets ? NUM.format(job.sheets) : '<span class="text-muted">—</span>'}</td>
                <td>
                    <span class="pill pill-${tone}">${label}</span>
                    ${job.unauthorized ? '<span class="pill pill-bad">Sin autorizar</span>' : ''}
                </td>
                <td class="text-right">
                    ${job.reprintable ? `<button class="btn btn-secondary btn-sm action-reprint" data-job-id="${escapeHtml(job.id)}"
                        data-printer="${escapeHtml(job.printer)}" data-user="${escapeHtml(job.user)}">Reimprimir</button>` : ''}
                </td>
            </tr>`;
        }).join('');
    }

    function formatDate(epoch) {
        if (!epoch) return '—';
        return new Date(epoch * 1000).toLocaleString('es-CL', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    }

    // --- EQUIPOS ---

    async function fetchUsers() {
        try {
            const { users } = await getJson(`/api/report?days=${$('users-days').value}`);
            const tbody = $('users-tbody');
            if (users.length === 0) {
                tbody.innerHTML = '<tr class="empty-row"><td colspan="5">Sin actividad en este período.</td></tr>';
                return;
            }
            tbody.innerHTML = users.map(user => `
                <tr>
                    <td>${escapeHtml(user.user)}</td>
                    <td class="text-right">${NUM.format(user.sheets)}</td>
                    <td class="text-right">${NUM.format(user.printed)}</td>
                    <td class="text-right">${NUM.format(user.denied)}</td>
                    <td class="text-muted nowrap">${escapeHtml(formatDate(user.last_at))}</td>
                </tr>`).join('');
        } catch (error) {
            console.error('Error cargando equipos:', error);
        }
    }

    // --- ACCIONES ---

    document.body.addEventListener('click', async (e) => {
        const target = e.target.closest('button');
        if (!target) return;
        const jobId = target.dataset.jobId;
        const user = target.dataset.user;

        if (target.classList.contains('action-preview')) {
            openPreview(jobId, Number(target.dataset.pages), target.dataset.name);
        } else if (target.classList.contains('action-approve')) {
            performAction('/approve', { job_id: jobId, user });
        } else if (target.classList.contains('action-deny')) {
            if (await showConfirmModal('Denegar impresión', `¿Denegar y eliminar el trabajo #${jobId} (${user})? No se imprimirá.`)) {
                performAction('/deny', { job_id: jobId });
            }
        } else if (target.classList.contains('action-cancel')) {
            if (await showConfirmModal('Cancelar impresión', `¿Cancelar el trabajo #${jobId}?`)) {
                performAction('/cancel', { job_id: jobId });
            }
        } else if (target.classList.contains('action-reprint')) {
            const printer = target.dataset.printer;
            if (await showConfirmModal('Reimprimir documento', `¿Reimprimir el trabajo #${jobId} (${user}) en "${printer}"?`)) {
                performAction('/reprint', { job_id: jobId, printer, user });
            }
        }
    });

    async function performAction(url, bodyData) {
        try {
            const formData = new FormData();
            for (const key in bodyData) formData.append(key, bodyData[key]);

            const response = await fetch(url, { method: 'POST', body: formData, headers: { 'X-Requested-With': 'XMLHttpRequest' } });
            if (response.status === 401) { window.location.href = '/login'; return; }
            if (!response.ok) throw new Error('Error del servidor');

            const result = await response.json();
            showToast(result.message, !result.success);
            if (result.success) {
                fetchStatus();
                refreshView();
            }
        } catch (error) {
            console.error('Error al realizar la acción:', error);
            showToast('No se pudo completar la acción.', true);
        }
    }

    function showToast(message, isError, duration = 4000) {
        const toast = $('toast');
        toast.textContent = message;
        toast.classList.toggle('toast-error', Boolean(isError));
        toast.hidden = false;
        clearTimeout(showToast.timer);
        showToast.timer = setTimeout(() => { toast.hidden = true; }, duration);
    }

    $('alert-ack').addEventListener('click', () => performAction('/alerts/ack', {}));
    $('alert-view').addEventListener('click', () => {
        $('history-status').value = 'unauthorized';
        $('history-days').value = '365';
        if (currentView === 'historial') fetchHistory();
    });

    // --- MODALES ---

    function showConfirmModal(title, message) {
        $('modal-title').textContent = title;
        $('modal-message').textContent = message;
        $('confirm-modal').classList.add('show');
        return new Promise(resolve => { modalResolver = resolve; });
    }

    function closeModal(result) {
        $('confirm-modal').classList.remove('show');
        if (modalResolver) {
            modalResolver(result);
            modalResolver = null;
        }
    }

    $('modal-btn-confirm').addEventListener('click', () => closeModal(true));
    $('modal-btn-cancel').addEventListener('click', () => closeModal(false));
    $('modal-close-btn').addEventListener('click', () => closeModal(false));
    $('confirm-modal').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeModal(false); });

    function openPreview(jobId, pages, name) {
        previewJob = { id: jobId, pages: pages || 1 };
        $('preview-title').textContent = name ? `#${jobId} · ${name}` : `Trabajo #${jobId}`;
        showPreviewPage(1);
        $('preview-modal').classList.add('show');
    }

    function showPreviewPage(page) {
        previewCurrentPage = page;
        $('preview-image').src = `/preview/${encodeURIComponent(previewJob.id)}.png?large=1&page=${page}`;
        $('preview-page').textContent = `Página ${page} de ${previewJob.pages}`;
        $('preview-prev').disabled = page <= 1;
        $('preview-next').disabled = page >= previewJob.pages;
    }

    function closePreview() {
        $('preview-modal').classList.remove('show');
        $('preview-image').removeAttribute('src');
        previewJob = null;
    }

    $('preview-prev').addEventListener('click', () => showPreviewPage(previewCurrentPage - 1));
    $('preview-next').addEventListener('click', () => showPreviewPage(previewCurrentPage + 1));
    $('preview-close-btn').addEventListener('click', closePreview);
    $('preview-modal').addEventListener('click', (e) => { if (e.target === e.currentTarget) closePreview(); });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeModal(false);
            if (previewJob) closePreview();
        }
        if (!previewJob) return;
        if (e.key === 'ArrowLeft' && previewCurrentPage > 1) showPreviewPage(previewCurrentPage - 1);
        if (e.key === 'ArrowRight' && previewCurrentPage < previewJob.pages) showPreviewPage(previewCurrentPage + 1);
    });

    // --- MODO REVISIÓN ---

    reviewToggle.addEventListener('change', async () => {
        const enabled = reviewToggle.checked;
        const confirmed = await showConfirmModal(
            enabled ? 'Activar modo revisión' : 'Desactivar modo revisión',
            enabled
                ? 'Cada nueva impresión quedará retenida hasta que la autorices desde este panel.'
                : 'Las nuevas impresiones se imprimirán directamente. Los trabajos que ya están esperando seguirán necesitando autorización.'
        );
        if (!confirmed) {
            reviewToggle.checked = !enabled;
            return;
        }
        performAction('/review-mode', { enabled: enabled ? '1' : '0' });
    });

    // --- TEMA CLARO / OSCURO ---

    $('theme-toggle').addEventListener('click', () => {
        const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('webcups-theme', next); } catch (e) {}
    });

    // Seguir el tema del sistema mientras el usuario no haya elegido uno
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
        let saved = null;
        try { saved = localStorage.getItem('webcups-theme'); } catch (err) {}
        if (!saved) document.documentElement.setAttribute('data-theme', e.matches ? 'light' : 'dark');
    });

    // --- UTILIDADES ---

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // --- INICIO ---

    $('report-days').addEventListener('change', fetchReport);
    $('users-days').addEventListener('change', fetchUsers);
    $('history-status').addEventListener('change', fetchHistory);
    $('history-days').addEventListener('change', fetchHistory);
    $('history-q').addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(fetchHistory, 250);
    });
    window.addEventListener('hashchange', showView);
    window.addEventListener('resize', () => { if (lastReport && currentView === 'resumen') drawDayChart(lastReport.by_day); });

    showView();
    fetchStatus();
    setInterval(fetchStatus, 3000);
    setInterval(() => { if (!document.hidden) refreshView(); }, 30000);
});
