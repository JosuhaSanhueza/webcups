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
        if (oldJobs.length !== newJobs.length) return true;
        for (let i = 0; i < oldJobs.length; i++) {
            if (oldJobs[i].id !== newJobs[i].id || 
                oldJobs[i].user !== newJobs[i].user || 
                oldJobs[i].printer !== newJobs[i].printer ||
                (oldJobs[i].date && oldJobs[i].date !== newJobs[i].date)) {
                return true;
            }
        }
        return false;
    }

    function renderActiveJobs(jobs) {
        if (jobs.length === 0) {
            activeTbody.innerHTML = `
                <tr class="empty-row">
                    <td colspan="4">No hay trabajos activos en este momento.</td>
                </tr>
            `;
            return;
        }

        activeTbody.innerHTML = jobs.map(job => `
            <tr data-job-id="${job.id}">
                <td class="font-mono text-cyan">${job.id}</td>
                <td>
                    <div class="user-cell">
                        <span class="avatar">👤</span>
                        <span>${escapeHtml(job.user)}</span>
                    </div>
                </td>
                <td><span class="printer-tag">${escapeHtml(job.printer)}</span></td>
                <td class="text-right">
                    <button class="btn btn-danger btn-sm action-cancel" data-job-id="${job.id}">
                        Cancelar
                    </button>
                </td>
            </tr>
        `).join('');
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
