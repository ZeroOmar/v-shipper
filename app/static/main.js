/* Main application JavaScript */

const API_BASE = '/api';
const THEME_KEY = 'vshipper_theme';
let currentUser = null;
let sessionId = null;
let refreshInterval = null;
let volumeSizePollInterval = null;
let poolsCache = {};
let poolsMetadata = {}; // Cache for volume/backup counts
let activePool = null;
let taskHistory = []; // Keep track of recent tasks
let consecutiveLoadFailures = 0; // Track network failures

// Configuration
const POLL_INTERVAL = 2000; // 2 seconds
const AUTO_REFRESH_INTERVAL = 30000; // 30 seconds
const MAX_TASK_HISTORY = 10; // Keep last 10 tasks
const MAX_LOAD_FAILURES = 3; // Logout after 3 consecutive failures

// ============ Initialization ============

document.addEventListener('DOMContentLoaded', () => {
    sessionId = localStorage.getItem('session_id');
    currentUser = localStorage.getItem('username');
    loadTheme();
    checkAuthStatus();
});

// ============ Authentication ============

async function checkAuthStatus() {
    // If no session, show login
    if (!sessionId) {
        showLoginScreen();
    } else {
        showDashboard();
    }
}

async function login() {
    const username = document.getElementById('username').value;
    const password = document.getElementById('password').value;
    
    if (!username || !password) {
        showError('Please enter username and password');
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            sessionId = data.session_id;
            currentUser = username;
            localStorage.setItem('session_id', sessionId);
            localStorage.setItem('username', username);
            showDashboard();
            loadPools();
        } else {
            showError(data.detail || 'Login failed');
        }
    } catch (error) {
        showError(`Login error: ${error.message}`);
    }
}

async function logout() {
    try {
        sessionId = null;
        localStorage.removeItem('session_id');
        if (refreshInterval) clearInterval(refreshInterval);
        showLoginScreen();
    } catch (error) {
        showError(`Logout error: ${error.message}`);
    }
}

// ============ UI State Management ============

function showLoginScreen() {
    document.getElementById('loginScreen').classList.add('active');
    document.getElementById('dashboard').classList.remove('active');
}

function showDashboard() {
    document.getElementById('loginScreen').classList.remove('active');
    document.getElementById('dashboard').classList.add('active');
    document.getElementById('currentUser').textContent = currentUser || 'admin';
    loadTaskHistory();
    loadPools();
    startAutoRefresh();
}

function startAutoRefresh() {
    if (refreshInterval) clearInterval(refreshInterval);
    refreshInterval = setInterval(() => {
        loadPools();
    }, AUTO_REFRESH_INTERVAL);
}

// ============ Pool Management ============

async function loadPools() {
    if (!sessionId) return;
    
    try {
        const response = await fetch(`${API_BASE}/pools?session_id=${sessionId}`);
        
        if (response.status === 401) {
            // Session expired
            sessionId = null;
            localStorage.removeItem('session_id');
            localStorage.removeItem('username');
            if (refreshInterval) clearInterval(refreshInterval);
            showLoginScreen();
            showError('Session expired. Please log in again.');
            return;
        }
        
        const data = await response.json();
        
        if (!response.ok) {
            showError(data.detail);
            return;
        }
        
        // Reset failure counter on successful load
        consecutiveLoadFailures = 0;
        
        poolsCache = {};
        data.pools.forEach(pool => {
            poolsCache[pool.name] = pool;
        });
        displayPools(data.pools);
    } catch (error) {
        consecutiveLoadFailures++;
        console.warn(`Load pools failure ${consecutiveLoadFailures}/${MAX_LOAD_FAILURES}: ${error.message}`);
        
        if (consecutiveLoadFailures >= MAX_LOAD_FAILURES) {
            // App is offline - redirect to login and stop refreshes
            showError('Application is offline. Please check your connection.');
            sessionId = null;
            localStorage.removeItem('session_id');
            localStorage.removeItem('username');
            if (refreshInterval) clearInterval(refreshInterval);
            showLoginScreen();
            return;
        }
        
        showError(`Failed to load pools: ${error.message}`);
    }
}

function displayPools(pools) {
    const container = document.getElementById('poolsList');
    container.innerHTML = '';
    
    pools.forEach(pool => {
        const poolEl = document.createElement('div');
        poolEl.className = 'pool-item';
        if (activePool === pool.name) poolEl.classList.add('active');
        
        const usagePercent = pool.usage_percent || 0;
        const metadata = poolsMetadata[pool.name];
        const countLabel = metadata
            ? pool.role === 'backup'
                ? `📦 ${metadata.backup_count} backups`
                : `📂 ${metadata.volume_count} volumes`
            : 'Loading counts...';
        
        poolEl.innerHTML = `
            <div style="cursor: pointer;" onclick="selectPool('${pool.name}')">
                <div class="pool-name">${pool.name}</div>
                <div class="pool-type">${pool.role === 'backup' ? 'Backup' : 'Docker'} (${pool.pool_type})</div>
                <div class="pool-counts" style="margin-top: 6px; font-size: 12px; color: #7f8c8d;">
                    ${countLabel}
                </div>
                <div class="pool-stats" style="margin-top: 8px;">
                    <div class="stat">
                        <div class="stat-label">Free</div>
                        <div class="stat-value">${pool.available_gb.toFixed(1)} GB</div>
                    </div>
                    <div class="stat">
                        <div class="stat-label">Usage</div>
                        <div class="stat-value">${usagePercent.toFixed(0)}%</div>
                    </div>
                </div>
                <div class="progress-bar" style="margin-top: 8px;">
                    <div class="progress-fill" style="width: ${Math.min(usagePercent, 100)}%"></div>
                </div>
            </div>
        `;
        
        container.appendChild(poolEl);
    });
}

function selectPool(poolName) {
    activePool = poolName;
    displayPools(Object.values(poolsCache));
    loadVolumesForPool(poolName);
}

async function loadVolumesForPool(poolName) {
    if (!sessionId) return;
    activePool = poolName;
    if (volumeSizePollInterval) {
        clearInterval(volumeSizePollInterval);
        volumeSizePollInterval = null;
    }

    const container = document.getElementById('volumesContainer');
    container.innerHTML = `
        <div class="placeholder">
            <div class="loading-spinner"></div>
            <p>Loading volumes...</p>
        </div>
    `;

    try {
        const response = await fetch(`${API_BASE}/volumes?pool=${poolName}&session_id=${sessionId}`);
        const data = await response.json();
        
        if (!response.ok) {
            showError(data.detail);
            return;
        }
        
        // Cache count metadata for the selected pool to avoid repeated full scans.
        const poolRole = poolsCache[poolName]?.role || 'docker';
        poolsMetadata[poolName] = {
            volume_count: poolRole === 'backup' ? 0 : data.volumes.length,
            backup_count: poolRole === 'backup' ? data.volumes.length : 0
        };
        
        displayPools(Object.values(poolsCache));
        displayVolumes(poolName, data.volumes, data.warnings || []);
        startVolumeSizePolling(poolName, data.volumes);
    } catch (error) {
        showError(`Failed to load volumes: ${error.message}`);
    }
}

function displayVolumes(poolName, volumes, warnings = []) {
    const poolRole = poolsCache[poolName]?.role || 'docker';
    const container = document.getElementById('volumesContainer');
    
    let html = `<h2>${poolName}</h2>`;
    if (warnings.length > 0) {
        html += `<div class="warning-banner">${warnings.map(w => `<div>${w}</div>`).join('')}</div>`;
    }
    
    if (volumes.length === 0) {
        html += '<div class="placeholder"><p>No volumes found in this pool</p></div>';
    } else {
        volumes.forEach(volume => {
            const sizeText = volume.size_loading
                ? `<span class="loading-spinner" style="width: 14px; height: 14px; border-width: 2px;"></span> Calculating...`
                : formatVolumeSize(volume.size_bytes, volume.size_gb);
            const created = volume.created_timestamp ? new Date(volume.created_timestamp * 1000).toLocaleDateString() : 'N/A';
            const backupCount = volume.backups && volume.backups.length > 0 ? volume.backups.length : 0;
            
            html += `
                <div class="volume-item">
                    <div class="volume-info">
                        <div class="volume-name">${volume.name}</div>
                        <div class="volume-details">
                            Size: ${sizeText} | Created: ${created}
                            ${backupCount ? ` | Backups: ${backupCount}` : ''}
                        </div>
                    </div>
                    <div class="volume-actions">
                        ${poolRole !== 'backup' ? `<button class="btn" style="font-size: 11px;" onclick="openMigrateModal('${poolName}', '${volume.name}')">Migrate</button>` : ''}
                        ${poolRole !== 'backup' ? `<button class="btn" style="font-size: 11px;" onclick="openBackupModal('${poolName}', '${volume.name}')">Backup</button>` : `<button class="btn" style="font-size: 11px;" onclick="openRestoreModal('${poolName}', '${volume.name}')">Restore</button>`}
                        <button class="btn danger" style="font-size: 11px;" onclick="openDeleteModal('${poolName}', '${volume.name}')">Delete</button>
                    </div>
                </div>
            `;
        });
    }
    
    container.innerHTML = html;
}

function startVolumeSizePolling(poolName, volumes) {
    if (!volumes.some(volume => volume.size_loading)) {
        return;
    }

    if (volumeSizePollInterval) {
        clearInterval(volumeSizePollInterval);
    }

    volumeSizePollInterval = setInterval(async () => {
        if (activePool !== poolName) {
            clearInterval(volumeSizePollInterval);
            volumeSizePollInterval = null;
            return;
        }

        try {
            const response = await fetch(`${API_BASE}/volumes?pool=${poolName}&session_id=${sessionId}`);
            const data = await response.json();

            if (!response.ok) {
                return;
            }

            displayVolumes(poolName, data.volumes, data.warnings || []);

            if (!data.volumes.some(volume => volume.size_loading)) {
                clearInterval(volumeSizePollInterval);
                volumeSizePollInterval = null;
            }
        } catch (error) {
            console.warn('Volume size refresh error:', error.message);
        }
    }, 2500);
}

// ============ Migration ============

function openMigrateModal(sourcePool, sourceVolume) {
    const modal = document.getElementById('migrateModal');
    const content = modal.querySelector('.modal-content');
    
    content.innerHTML = `
        <div class="modal-header">
            <h3>Migrate Volume</h3>
            <button class="close-btn" onclick="closeModal('migrateModal')">×</button>
        </div>
        <div class="form-group">
            <label>Source Pool</label>
            <input type="text" value="${sourcePool}" disabled>
        </div>
        <div class="form-group">
            <label>Source Volume</label>
            <input type="text" value="${sourceVolume}" disabled>
        </div>
        <div class="form-group">
            <label>Destination Pool</label>
            <select id="destPool">
                <option value="">-- Select pool --</option>
            </select>
        </div>
        <div class="form-group">
            <label class="checkbox-label">
                <input type="checkbox" id="migrateVerify" checked>
                <span>Verify migration</span>
            </label>
        </div>
        <div class="form-group">
            <label class="checkbox-label">
                <input type="checkbox" id="migrateDelete">
                <span>Delete source after verification</span>
            </label>
        </div>
        <button class="btn success" style="width: 100%;" onclick="startMigration('${sourcePool}', '${sourceVolume}')">Start Migration</button>
    `;
    
    // Load pools for destination
    loadPoolsForSelect('destPool');
    openModal('migrateModal');
}

async function startMigration(sourcePool, sourceVolume) {
    const destPool = document.getElementById('destPool').value;
    const verify = document.getElementById('migrateVerify').checked;
    const deleteSource = document.getElementById('migrateDelete').checked;
    
    if (!destPool) {
        showError('Please select destination pool');
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/migrate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_pool: sourcePool,
                source_volume: sourceVolume,
                dest_pool: destPool,
                verify: verify,
                delete_source: deleteSource
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            closeModal('migrateModal');
            showTaskProgress(data.task_id);
        } else {
            showError(data.detail);
        }
    } catch (error) {
        showError(`Migration error: ${error.message}`);
    }
}

// ============ Backup ============

function openBackupModal(sourcePool, sourceVolume) {
    const modal = document.getElementById('backupModal');
    const content = modal.querySelector('.modal-content');
    
    content.innerHTML = `
        <div class="modal-header">
            <h3>Backup Volume</h3>
            <button class="close-btn" onclick="closeModal('backupModal')">×</button>
        </div>
        <div class="form-group">
            <label>Source Pool</label>
            <input type="text" value="${sourcePool}" disabled>
        </div>
        <div class="form-group">
            <label>Source Volume</label>
            <input type="text" value="${sourceVolume}" disabled>
        </div>
        <div class="form-group">
            <label>Backup Pool</label>
            <select id="backupPool">
                <option value="">-- Select backup pool --</option>
            </select>
        </div>
        <div class="form-group">
            <label class="checkbox-label">
                <input type="checkbox" id="backupVerify" checked>
                <span>Verify backup</span>
            </label>
        </div>
        <button class="btn success" style="width: 100%;" onclick="startBackup('${sourcePool}', '${sourceVolume}')">Start Backup</button>
    `;
    
    // Load backup pools
    loadBackupPoolsForSelect('backupPool');
    openModal('backupModal');
}

async function startBackup(sourcePool, sourceVolume) {
    const backupPool = document.getElementById('backupPool').value;
    const verify = document.getElementById('backupVerify').checked;
    
    if (!backupPool) {
        showError('Please select backup pool');
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/backup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_pool: sourcePool,
                source_volume: sourceVolume,
                backup_pool: backupPool,
                verify: verify
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            closeModal('backupModal');
            showTaskProgress(data.task_id);
        } else {
            showError(data.detail);
        }
    } catch (error) {
        showError(`Backup error: ${error.message}`);
    }
}

function openRestoreModal(backupPool, backupFile) {
    const modal = document.getElementById('backupModal');
    const content = modal.querySelector('.modal-content');
    const defaultVolume = backupFile.split('_backup_')[0] || backupFile.replace(/\.tar\.gz$/, '');

    content.innerHTML = `
        <div class="modal-header">
            <h3>Restore Backup</h3>
            <button class="close-btn" onclick="closeModal('backupModal')">×</button>
        </div>
        <div class="form-group">
            <label>Backup Pool</label>
            <input type="text" value="${backupPool}" disabled>
        </div>
        <div class="form-group">
            <label>Backup File</label>
            <input type="text" value="${backupFile}" disabled>
        </div>
        <div class="form-group">
            <label>Destination Pool</label>
            <select id="restorePool">
                <option value="">-- Select destination pool --</option>
            </select>
        </div>
        <div class="form-group">
            <label>Volume Name</label>
            <input type="text" id="restoreVolumeName" value="${defaultVolume}" required>
        </div>
        <button class="btn success" style="width: 100%;" onclick="startRestore('${backupPool}', '${backupFile}')">Start Restore</button>
    `;

    loadPoolsForSelect('restorePool');
    openModal('backupModal');
}

async function startRestore(backupPool, backupFile) {
    const destPool = document.getElementById('restorePool').value;
    const destVolume = document.getElementById('restoreVolumeName').value;

    if (!destPool || !destVolume) {
        showError('Please select a destination pool and volume name');
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/restore`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                backup_pool: backupPool,
                backup_file: backupFile,
                dest_pool: destPool,
                dest_volume: destVolume
            })
        });

        const data = await response.json();

        if (response.ok) {
            closeModal('backupModal');
            showTaskProgress(data.task_id);
        } else {
            showError(data.detail);
        }
    } catch (error) {
        showError(`Restore error: ${error.message}`);
    }
}

// ============ Delete ============

function openDeleteModal(pool, volume) {
    const modal = document.getElementById('deleteModal');
    const content = modal.querySelector('.modal-content');
    
    content.innerHTML = `
        <div class="modal-header">
            <h3>Delete Volume</h3>
            <button class="close-btn" onclick="closeModal('deleteModal')">×</button>
        </div>
        <p><strong>Warning:</strong> This action cannot be undone. Make sure you have a backup if needed.</p>
        <div class="form-group">
            <label>Pool</label>
            <input type="text" value="${pool}" disabled>
        </div>
        <div class="form-group">
            <label>Volume</label>
            <input type="text" value="${volume}" disabled>
        </div>
        <label class="checkbox-label">
            <input type="checkbox" id="deleteConfirm">
            <span>Yes, I want to delete this volume</span>
        </label>
        <button class="btn danger" id="confirmDeleteButton" style="width: 100%; margin-top: 15px;" onclick="confirmDelete('${pool}', '${volume}')">Delete</button>
    `;
    
    openModal('deleteModal');
}

async function confirmDelete(pool, volume) {
    const confirmed = document.getElementById('deleteConfirm').checked;
    
    if (!confirmed) {
        showError('Please confirm deletion');
        return;
    }

    const deleteButton = document.getElementById('confirmDeleteButton');
    if (deleteButton) {
        deleteButton.disabled = true;
        deleteButton.textContent = 'Deleting...';
    }
    
    try {
        const response = await fetch(`${API_BASE}/delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                pool: pool,
                volume_name: volume,
                confirm: confirmed
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            if (data.require_confirmation) {
                showError(data.message || 'Deletion requires confirmation');
                return;
            }

            closeModal('deleteModal');
            addOrUpdateTaskHistory({
                task_id: data.task_id,
                status: 'pending',
                current_operation: 'Deleting volume...',
                progress_percent: 0,
                elapsed_seconds: 0,
                estimated_remaining_seconds: null
            });
            renderTaskHistory();
            showTaskProgress(data.task_id);
            loadPools();
            if (activePool) {
                loadVolumesForPool(activePool);
            }
        } else {
            showError(data.detail);
        }
    } catch (error) {
        showError(`Delete error: ${error.message}`);
    } finally {
        if (deleteButton) {
            deleteButton.disabled = false;
            deleteButton.textContent = 'Delete';
        }
    }
}

// ============ Task Progress ============

// Global tracking of current task
let currentTaskId = null;
let currentTaskPollInterval = null;

async function showTaskProgress(taskId) {
    currentTaskId = taskId;
    
    // Insert initial task entry
    addOrUpdateTaskHistory({
        task_id: taskId,
        current_operation: 'Starting task...',
        progress_percent: 0,
        status: 'pending',
        elapsed_seconds: 0,
        estimated_remaining_seconds: null
    });
    renderTaskHistory();
    
    // Start polling progress
    currentTaskPollInterval = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/task/${taskId}/progress`);
            const data = await response.json();
            
            if (!response.ok) {
                const errorText = data.detail || 'Unable to load task progress.';
                addOrUpdateTaskHistory({
                    task_id: taskId,
                    current_operation: 'Task state unavailable',
                    progress_percent: 0,
                    status: 'failed',
                    elapsed_seconds: 0,
                    estimated_remaining_seconds: null,
                    error: errorText
                });
                renderTaskHistory();
                clearInterval(currentTaskPollInterval);
                currentTaskId = null;
                return;
            }

            addOrUpdateTaskHistory(data);
            renderTaskHistory();
            
            if (data.status === 'completed' || data.status === 'failed') {
                clearInterval(currentTaskPollInterval);
                currentTaskId = null;
                setTimeout(() => {
                    loadPools();
                    if (activePool) {
                        loadVolumesForPool(activePool);
                    }
                }, 2000);
            }
        } catch (error) {
            console.error('Progress poll error:', error);
        }
    }, POLL_INTERVAL);
}

function addOrUpdateTaskHistory(task) {
    const index = taskHistory.findIndex(entry => entry.task_id === task.task_id);
    const newEntry = {
        task_id: task.task_id,
        task_type: task.task_type || task.type || 'operation',
        current_operation: task.current_operation || 'Processing...',
        progress_percent: task.progress_percent || 0,
        status: task.status || 'pending',
        elapsed_seconds: task.elapsed_seconds || 0,
        estimated_remaining_seconds: task.estimated_remaining_seconds || null,
        error: task.error || null,
        params: task.params || {}
    };
    
    if (index === -1) {
        taskHistory.unshift(newEntry);
    } else {
        taskHistory[index] = newEntry;
    }
    
    taskHistory = taskHistory.slice(0, MAX_TASK_HISTORY);
}

function getTaskTargetLabel(task) {
    const params = task.params || {};
    if (task.task_type === 'delete') {
        return params.volume_name || params.source_volume || params.volume || 'Delete operation';
    }
    if (task.task_type === 'backup') {
        if (params.source_volume) {
            return `Backup ${params.source_volume} to ${params.backup_pool || 'backup pool'}`;
        }
        return 'Backup task';
    }
    if (task.task_type === 'migrate') {
        if (params.source_volume) {
            return `Migrate ${params.source_volume} from ${params.source_pool || 'unknown'} to ${params.dest_pool || 'unknown'}`;
        }
        return 'Migration task';
    }
    if (task.task_type === 'restore') {
        const backupFile = params.backup_file || params.source_volume;
        return backupFile
            ? `Restore ${backupFile} to ${params.dest_volume_name || params.dest_volume || 'destination'}`
            : 'Restore task';
    }
    return params.source_volume || params.volume_name || params.backup_file || task.current_operation || 'Task';
}

function renderTaskHistory() {
    const progressPanel = document.getElementById('progressPanel');
    
    if (taskHistory.length === 0) {
        progressPanel.innerHTML = '<div class="placeholder-text">No active tasks</div>';
        return;
    }
    
    const items = taskHistory.map(task => {
        const targetLabel = getTaskTargetLabel(task);
        return `
            <div class="task-history-item">
                <div class="task-status ${task.status}">${task.status.toUpperCase()}</div>
                <div class="progress-text"><strong>${task.task_type || 'Task'}</strong>: ${task.current_operation}</div>
                <div class="progress-text task-target">${targetLabel}</div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${task.progress_percent}%"></div>
                </div>
                <div class="task-meta">${task.progress_percent}% · ${task.elapsed_seconds}s${task.estimated_remaining_seconds ? ` · ${task.estimated_remaining_seconds}s left` : ''}</div>
                ${task.error ? `<div class="error-message">${task.error}</div>` : ''}
            </div>
        `;
    }).join('');
    
    progressPanel.innerHTML = items;
}

function formatVolumeSize(sizeBytes, fallbackGb = 0) {
    if ((typeof sizeBytes !== 'number' || isNaN(sizeBytes) || sizeBytes < 0) && fallbackGb > 0) {
        return formatVolumeSize(Math.round(fallbackGb * 1024 ** 3));
    }

    if (typeof sizeBytes !== 'number' || isNaN(sizeBytes) || sizeBytes < 0) {
        return '0.00 GB';
    }

    if (sizeBytes === 0) {
        if (fallbackGb > 0) {
            return formatVolumeSize(Math.round(fallbackGb * 1024 ** 3));
        }
        return '0.00 GB';
    }

    const units = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    let value = sizeBytes;
    let unitIndex = 0;

    while (value >= 1024 && unitIndex < units.length - 1) {
        value /= 1024;
        unitIndex += 1;
    }

    return `${value.toFixed(2)} ${units[unitIndex]}`;
}

async function loadTaskHistory() {
    if (!sessionId) return;

    try {
        const response = await fetch(`${API_BASE}/tasks?session_id=${sessionId}`);
        const data = await response.json();

        if (!response.ok) {
            return;
        }

        taskHistory = data.tasks.map(task => ({
            task_id: task.task_id,
            task_type: task.task_type || 'operation',
            current_operation: task.current_operation || 'Processing...',
            progress_percent: task.progress_percent || 0,
            status: task.status || 'pending',
            elapsed_seconds: task.elapsed_seconds || 0,
            estimated_remaining_seconds: task.estimated_remaining_seconds || null,
            error: task.error || null,
            params: task.params || {}
        }));
        taskHistory = taskHistory.slice(0, MAX_TASK_HISTORY);
        renderTaskHistory();
    } catch (error) {
        console.warn('Failed to load task history:', error.message);
    }
}

// ============ Helper Functions ============

async function loadPoolsForSelect(selectId) {
    try {
        const response = await fetch(`${API_BASE}/pools?session_id=${sessionId}`);
        const data = await response.json();
        
        const select = document.getElementById(selectId);
        select.innerHTML = '<option value="">-- Select pool --</option>';
        
        data.pools.forEach(pool => {
            if (pool.pool_type !== 'backup') {
                const option = document.createElement('option');
                option.value = pool.name;
                option.textContent = `${pool.name} (${pool.available_gb.toFixed(1)} GB free)`;
                select.appendChild(option);
            }
        });
    } catch (error) {
        console.error('Failed to load pools:', error);
    }
}

async function loadBackupPoolsForSelect(selectId) {
    try {
        const response = await fetch(`${API_BASE}/pools?session_id=${sessionId}`);
        const data = await response.json();
        
        const select = document.getElementById(selectId);
        select.innerHTML = '<option value="">-- Select backup pool --</option>';
        
        data.pools.forEach(pool => {
            if (pool.pool_type === 'backup') {
                const option = document.createElement('option');
                option.value = pool.name;
                option.textContent = `${pool.name} (${pool.available_gb.toFixed(1)} GB free)`;
                select.appendChild(option);
            }
        });
    } catch (error) {
        console.error('Failed to load backup pools:', error);
    }
}

function openModal(modalId) {
    document.getElementById(modalId).classList.add('active');
}

function loadTheme() {
    const savedTheme = localStorage.getItem(THEME_KEY) || 'system';
    if (savedTheme === 'system') {
        const darkMode = window.matchMedia('(prefers-color-scheme: dark)').matches;
        applyTheme(darkMode ? 'dark' : 'light');
    } else {
        applyTheme(savedTheme);
    }
    updateThemeButton(savedTheme);
}

function toggleTheme() {
    const current = localStorage.getItem(THEME_KEY) || 'system';
    const next = current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system';
    localStorage.setItem(THEME_KEY, next);
    if (next === 'system') {
        const darkMode = window.matchMedia('(prefers-color-scheme: dark)').matches;
        applyTheme(darkMode ? 'dark' : 'light');
    } else {
        applyTheme(next);
    }
    updateThemeButton(next);
}

function applyTheme(theme) {
    document.body.classList.remove('light', 'dark');
    document.body.classList.add(theme === 'dark' ? 'dark' : 'light');
}

function updateThemeButton(theme) {
    const button = document.getElementById('themeToggleButton');
    if (button) {
        button.textContent = `🎨 ${theme.charAt(0).toUpperCase() + theme.slice(1)}`;
    }
}

async function runTroubleshootCleanup() {
    try {
        const response = await fetch(`${API_BASE}/debug/cleanup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await response.json();

        if (response.ok) {
            showSuccess(`Cleanup complete: ${data.deleted_tasks_file} tasks file, ${data.deleted_lock_files} lock files removed`);
            loadTaskHistory();
        } else {
            showError(data.detail || 'Cleanup failed');
        }
    } catch (error) {
        showError(`Cleanup error: ${error.message}`);
    }
}

function closeModal(modalId) {
    document.getElementById(modalId).classList.remove('active');
}

function showError(message) {
    createToast(message, 'error');
}

function showSuccess(message) {
    createToast(message, 'success');
}

function createToast(message, type) {
    const toastContainer = document.getElementById('toastContainer');
    if (!toastContainer) {
        console.warn('Toast container not found');
        return;
    }

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    toastContainer.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 5000);
}

// Close modals on background click
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.remove('active');
    }
});

// Restore session if available
