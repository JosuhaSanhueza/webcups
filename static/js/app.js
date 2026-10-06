document.addEventListener('DOMContentLoaded', () => {
    // Referencias al DOM
    const countActive = document.getElementById('count-active');
    const countHistory = document.getElementById('count-history');
    const activeBadge = document.getElementById('active-badge');
    const historyBadge = document.getElementById('history-badge');
    const printerStatus = document.getElementById('printer-status-text');
    const activeTbody = document.getElementById('active-jobs-tbody');
    const historyTbody = document.getElementById('history-jobs-tbody');
    
    // Modal
    const confirmModal = document.getElementById('confirm-modal');
    const modalTitle = document.getElementById('modal-title');
    const modalMessage = document.getElementById('modal-message');
    const modalCloseBtn = document.getElementById('modal-close-btn');
    const modalBtnCancel = document.getElementById('modal-btn-cancel');
    const modalBtnConfirm = document.getElementById('modal-btn-confirm');

    // Vista previa
    const previewModal = document.getElementById('preview-modal');
    const previewTitle = document.getElementById('preview-title');
    const previewImage = document.getElementById('preview-image');
    const previewPage = document.getElementById('preview-page');
    const previewPrev = document.getElementById('preview-prev');
    const previewNext = document.getElementById('preview-next');
    const previewCloseBtn = document.getElementById('preview-close-btn');
    const reviewToggle = document.getElementById('review-mode-toggle');
    let previewJob = null;
    let previewCurrentPage = 1;
    
    // Estado interno para evitar repintados innecesarios
    let currentActiveJobs = [];
    let currentHistoryJobs = [];
    let modalResolver = null;

    // Iniciar polling silencioso rápido cada 3 segundos
    setInterval(fetchStatus, 3000);
    // Primera carga inmediata
    fetchStatus();

    // Configuración de eventos de tabla dinámica
    setupTableListeners();

    // --- FUNCIONES DEL DASHBOARD ---

    // Actualiza la interfaz gráfica con nuevos datos
    function updateUI(data) {
        // Actualizar contadores
        countActive.textContent = data.active.length;
        countHistory.textContent = data.history.length;
        activeBadge.textContent = `${data.active.length} en cola`;
        historyBadge.textContent = `Últimos ${data.history.length}`;
        
        // Actualizar estado de la impresora
        printerStatus.textContent = data.status_text;
        printerStatus.className = `status-indicator ${data.status_class}`;

        // Estado del modo revisión
        reviewToggle.checked = data.review_mode;

        // Actualizar tabla de activos (si hay cambios)
        if (hasJobsChanged(currentActiveJobs, data.active)) {
            currentActiveJobs = data.active;
            renderActiveJobs(data.active);
        }

        // Actualizar tabla de historial (si hay cambios)
        if (hasJobsChanged(currentHistoryJobs, data.history)) {
            currentHistoryJobs = data.history;
            renderHistoryJobs(data.history);
        }
    }

    // Consulta el estado del servidor de manera asíncrona
    async function fetchStatus() {
        try {
            const response = await fetch('/api/status');
            if (!response.ok) throw new Error('Error de red al consultar API');
            const data = await response.json();
            updateUI(data);
        } catch (error) {
            console.error('Error actualizando dashboard:', error);
        }
    }

    // Compara si dos arreglos de trabajos de impresión son distintos
    function hasJobsChanged(oldJobs, newJobs) {
        return JSON.stringify(oldJobs) !== JSON.stringify(newJobs);
    }

    function renderActiveJobs(jobs) {
        if (jobs.length === 0) {
            activeTbody.innerHTML = `
                <tr class="empty-row">
                    <td colspan="6">No hay trabajos activos en este momento.</td>
                </tr>
            `;
            return;
        }

        activeTbody.innerHTML = jobs.map(job => `
            <tr data-job-id="${job.id}" class="${job.held ? 'row-held' : ''}">
                <td>${renderThumb(job)}</td>
                <td class="font-mono text-cyan">${job.id}</td>
                <td>
                    <div class="user-cell">
                        <span class="avatar">👤</span>
                        <div>
                            <div>${escapeHtml(job.user)}</div>
                            ${job.name ? `<div class="job-name" title="${escapeHtml(job.name)}">${escapeHtml(job.name)}</div>` : ''}
                        </div>
                    </div>
                </td>
                <td><span class="printer-tag">${escapeHtml(job.printer)}</span></td>
                <td>${renderSheets(job)}</td>
                <td class="text-right">
                    ${job.held ? `
                        <div class="action-group">
                            <button class="btn btn-success btn-sm action-approve" data-job-id="${job.id}" data-user="${escapeHtml(job.user)}">
                                Autorizar
                            </button>
                            <button class="btn btn-danger btn-sm action-deny" data-job-id="${job.id}" data-user="${escapeHtml(job.user)}">
                                Denegar
                            </button>
                        </div>
                    ` : `
                        <button class="btn btn-danger btn-sm action-cancel" data-job-id="${job.id}">
                            Cancelar
                        </button>
                    `}
                </td>
            </tr>
        `).join('');
    }

    function renderThumb(job) {
        if (!job.preview) {
            return `<div class="thumb thumb-empty" title="Formato sin vista previa">📄</div>`;
        }
        return `
            <button class="thumb action-preview" data-job-id="${job.id}" data-pages="${job.pages || 1}"
                    data-name="${escapeHtml(job.name || '')}" title="Ver documento">
                <img src="/preview/${job.id}.png" alt="Página 1 del trabajo ${job.id}" loading="lazy">
            </button>
        `;
    }

    function renderSheets(job) {
        if (!job.sheets) {
            return `<span class="text-muted">—</span>`;
        }
        const details = [`${job.pages} pág.`];
        if (job.copies > 1) details.push(`${job.copies} copias`);
        if (job.duplex) details.push('doble cara');
        return `
            <div class="sheets-count">${job.sheets}</div>
            <div class="sheets-detail">${details.join(' · ')}</div>
        `;
    }

    function renderHistoryJobs(jobs) {
        if (jobs.length === 0) {
            historyTbody.innerHTML = `
                <tr class="empty-row">
                    <td colspan="5">El historial de impresión está vacío.</td>
                </tr>
            `;
            return;
        }

        historyTbody.innerHTML = jobs.map(job => `
            <tr data-job-id="${job.id}">
                <td class="font-mono">${job.id}</td>
                <td>
                    <div class="user-cell">
                        <span class="avatar">👤</span>
                        <span>${escapeHtml(job.user)}</span>
                    </div>
                </td>
                <td><span class="printer-tag">${escapeHtml(job.printer)}</span></td>
                <td class="text-muted">${escapeHtml(job.date)}</td>
                <td class="text-right">
                    <button class="btn btn-primary btn-sm action-reprint" 
                            data-job-id="${job.id}" 
                            data-printer="${escapeHtml(job.printer)}"
                            data-user="${escapeHtml(job.user)}">
                        Reimprimir
                    </button>
                </td>
            </tr>
        `).join('');
    }

    function setupTableListeners() {
        document.body.addEventListener('click', async (e) => {
            // Vista previa
            const thumb = e.target.closest('.action-preview');
            if (thumb) {
                openPreview(thumb.getAttribute('data-job-id'), Number(thumb.getAttribute('data-pages')), thumb.getAttribute('data-name'));
                return;
            }

            // Autorizar
            if (e.target.classList.contains('action-approve')) {
                performAction('/approve', {
                    job_id: e.target.getAttribute('data-job-id'),
                    user: e.target.getAttribute('data-user')
                });
            }

            // Denegar
            if (e.target.classList.contains('action-deny')) {
                const jobId = e.target.getAttribute('data-job-id');
                const user = e.target.getAttribute('data-user');
                const confirmed = await showConfirmModal(
                    'Denegar Impresión',
                    `¿Deseas denegar y eliminar el trabajo #${jobId} (Usuario: ${user})? No se imprimirá.`
                );
                if (confirmed) {
                    performAction('/deny', { job_id: jobId });
                }
            }

            // Cancelar
            if (e.target.classList.contains('action-cancel')) {
                const jobId = e.target.getAttribute('data-job-id');
                const confirmed = await showConfirmModal(
                    'Cancelar Impresión',
                    `¿Estás seguro de que deseas cancelar el trabajo #${jobId}?`
                );
                if (confirmed) {
                    performAction('/cancel', { job_id: jobId });
                }
            }
            
            // Reimprimir
            if (e.target.classList.contains('action-reprint')) {
                const jobId = e.target.getAttribute('data-job-id');
                const printer = e.target.getAttribute('data-printer');
                const user = e.target.getAttribute('data-user');
                const confirmed = await showConfirmModal(
                    'Reimprimir Documento',
                    `¿Deseas enviar a reimprimir el trabajo #${jobId} (Usuario: ${user}) en la impresora "${printer}"?`
                );
                if (confirmed) {
                    performAction('/reprint', { job_id: jobId, printer, user });
                }
            }
        });
    }

    // Realiza peticiones POST asíncronas en segundo plano
    async function performAction(url, bodyData) {
        try {
            const formData = new FormData();
            for (const key in bodyData) {
                formData.append(key, bodyData[key]);
            }

            const response = await fetch(url, {
                method: 'POST',
                body: formData,
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });

            if (!response.ok) throw new Error('Error del servidor');
            
            const result = await response.json();
            
            if (result.success) {
                // Forzar actualización inmediata tras acción exitosa
                fetchStatus();
            } else {
                console.error('La acción no se completó con éxito:', result.message);
            }
        } catch (error) {
            console.error('Error al realizar la acción de fondo:', error);
        }
    }

    // --- MODAL DE CONFIRMACIÓN ---

    function showConfirmModal(title, message) {
        modalTitle.textContent = title;
        modalMessage.textContent = message;
        confirmModal.classList.add('show');
        
        return new Promise((resolve) => {
            modalResolver = resolve;
        });
    }

    function closeModal(result) {
        confirmModal.classList.remove('show');
        if (modalResolver) {
            modalResolver(result);
            modalResolver = null;
        }
    }

    // Cerrar modal con botones
    modalBtnConfirm.addEventListener('click', () => closeModal(true));
    modalBtnCancel.addEventListener('click', () => closeModal(false));
    modalCloseBtn.addEventListener('click', () => closeModal(false));
    
    // Cerrar al hacer clic fuera del modal
    confirmModal.addEventListener('click', (e) => {
        if (e.target === confirmModal) {
            closeModal(false);
        }
    });

    // --- MODO REVISIÓN ---

    reviewToggle.addEventListener('change', async () => {
        const enabled = reviewToggle.checked;
        const confirmed = await showConfirmModal(
            enabled ? 'Activar Modo Revisión' : 'Desactivar Modo Revisión',
            enabled
                ? 'Cada nueva impresión quedará retenida hasta que la autorices desde este panel.'
                : 'Las nuevas impresiones se imprimirán directamente sin revisión. Los trabajos ya retenidos seguirán esperando autorización.'
        );
        if (!confirmed) {
            reviewToggle.checked = !enabled;
            return;
        }
        performAction('/review-mode', { enabled: enabled ? '1' : '0' });
    });

    // --- VISTA PREVIA ---

    function openPreview(jobId, pages, name) {
        previewJob = { id: jobId, pages: pages || 1 };
        previewTitle.textContent = name ? `#${jobId} · ${name}` : `Trabajo #${jobId}`;
        showPreviewPage(1);
        previewModal.classList.add('show');
    }

    function showPreviewPage(page) {
        previewCurrentPage = page;
        previewImage.src = `/preview/${previewJob.id}.png?large=1&page=${page}`;
        previewPage.textContent = `Página ${page} de ${previewJob.pages}`;
        previewPrev.disabled = page <= 1;
        previewNext.disabled = page >= previewJob.pages;
    }

    function closePreview() {
        previewModal.classList.remove('show');
        previewImage.removeAttribute('src');
        previewJob = null;
    }

    previewPrev.addEventListener('click', () => showPreviewPage(previewCurrentPage - 1));
    previewNext.addEventListener('click', () => showPreviewPage(previewCurrentPage + 1));
    previewCloseBtn.addEventListener('click', closePreview);
    previewModal.addEventListener('click', (e) => {
        if (e.target === previewModal) closePreview();
    });

    // --- TEMA CLARO / OSCURO ---

    const themeToggle = document.getElementById('theme-toggle');
    const prefersLight = window.matchMedia('(prefers-color-scheme: light)');

    themeToggle.addEventListener('click', () => {
        const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('webcups-theme', next); } catch (e) {}
    });

    // Seguir el tema del sistema mientras el usuario no haya elegido uno
    prefersLight.addEventListener('change', (e) => {
        let saved = null;
        try { saved = localStorage.getItem('webcups-theme'); } catch (err) {}
        if (!saved) {
            document.documentElement.setAttribute('data-theme', e.matches ? 'light' : 'dark');
        }
    });

    // --- UTILIDADES ---

    function escapeHtml(str) {
        if (typeof str !== 'string') return str;
        return str
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }
});
