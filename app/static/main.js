/* Main application JavaScript */

const API_BASE = '/api';
let currentUser = null;
let sessionId = null;
let refreshInterval = null;

// Configuration
const POLL_INTERVAL = 2000; // 2 seconds
const AUTO_REFRESH_INTERVAL = 30000; // 30 seconds

// ============ Initialization ============

document.addEventListener('DOMContentLoaded', () => {
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
        const data = await response.json();
        
        if (!response.ok) {
            showError(data.detail);
            return;
        }
        
        displayPools(data.pools);
    } catch (error) {
        showError(`Failed to load pools: ${error.message}`);
    }
}

function displayPools(pools) {
    const container = document.getElementById('poolsList');
    container.innerHTML = '';
    
    pools.forEach(pool => {
        const poolEl = document.createElement('div');
        poolEl.className = 'pool-item';
        
        const usagePercent = pool.usage_percent || 0;
        
        poolEl.innerHTML = `
            <div class="pool-header">
                <div>
                    <span class="pool-name">${pool.name}</span>
                    <span class="pool-type">${pool.pool_type}</span>
                </div>
                <button class="btn" onclick="loadVolumesForPool('${pool.name}')">View Volumes</button>
            </div>
            <div class="pool-stats">
                <div class="stat">
                    <div class="stat-label">Total</div>
                    <div class="stat-value">${pool.total_gb.toFixed(1)} GB</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Used</div>
                    <div class="stat-value">${pool.used_gb.toFixed(1)} GB</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Available</div>
                    <div class="stat-value">${pool.available_gb.toFixed(1)} GB</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Usage</div>
                    <div class="stat-value">${usagePercent.toFixed(1)}%</div>
                </div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: ${Math.min(usagePercent, 100)}%"></div>
            </div>
        `;
        
        container.appendChild(poolEl);
    });
}

async function loadVolumesForPool(poolName) {
    if (!sessionId) return;
    
    try {
        const response = await fetch(`${API_BASE}/volumes?pool=${poolName}&session_id=${sessionId}`);
        const data = await response.json();
        
        if (!response.ok) {
            showError(data.detail);
            return;
        }
        
        displayVolumes(poolName, data.volumes);
    } catch (error) {
        showError(`Failed to load volumes: ${error.message}`);
    }
}

function displayVolumes(poolName, volumes) {
    const modal = document.getElementById('volumesModal');
    const content = modal.querySelector('.modal-content');
    
    let html = `
        <div class="modal-header">
            <h3>Volumes in ${poolName}</h3>
            <button class="close-btn" onclick="closeModal('volumesModal')">×</button>
        </div>
        <div class="modal-body">
    `;
    
    if (volumes.length === 0) {
        html += '<p>No volumes found in this pool.</p>';
    } else {
        volumes.forEach(volume => {
            const sizeGB = (volume.size_gb || 0).toFixed(2);
            const created = volume.created_timestamp ? new Date(volume.created_timestamp * 1000).toLocaleDateString() : 'N/A';
            
            html += `
                <div class="volume-item">
                    <div class="volume-info">
                        <div class="volume-name">${volume.name}</div>
                        <div class="volume-details">
                            Size: ${sizeGB} GB | Created: ${created}
                            ${volume.backups && volume.backups.length > 0 ? ` | Backups: ${volume.backups.length}` : ''}
                        </div>
                    </div>
                    <div class="volume-actions">
                        <button class="btn" style="font-size: 11px;" onclick="openMigrateModal('${poolName}', '${volume.name}')">Migrate</button>
                        <button class="btn" style="font-size: 11px;" onclick="openBackupModal('${poolName}', '${volume.name}')">Backup</button>
                        <button class="btn danger" style="font-size: 11px;" onclick="openDeleteModal('${poolName}', '${volume.name}')">Delete</button>
                    </div>
                </div>
            `;
        });
    }
    
    html += '</div>';
    content.innerHTML = html;
    openModal('volumesModal');
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
            <label>
                <input type="checkbox" id="migrateVerify" checked> Verify migration
            </label>
        </div>
        <div class="form-group">
            <label>
                <input type="checkbox" id="migrateDelete"> Delete source after verification
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
            <label>
                <input type="checkbox" id="backupVerify" checked> Verify backup
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
        <label>
            <input type="checkbox" id="deleteConfirm"> Yes, I want to delete this volume
        </label>
        <button class="btn danger" style="width: 100%; margin-top: 15px;" onclick="confirmDelete('${pool}', '${volume}')">Delete</button>
    `;
    
    openModal('deleteModal');
}

async function confirmDelete(pool, volume) {
    if (!document.getElementById('deleteConfirm').checked) {
        showError('Please confirm deletion');
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                pool: pool,
                volume_name: volume
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            closeModal('deleteModal');
            showSuccess('Volume deleted successfully');
            loadPools();
        } else {
            showError(data.detail);
        }
    } catch (error) {
        showError(`Delete error: ${error.message}`);
    }
}

// ============ Task Progress ============

async function showTaskProgress(taskId) {
    const modal = document.getElementById('progressModal');
    const content = modal.querySelector('.modal-content');
    
    content.innerHTML = `
        <div class="modal-header">
            <h3>Operation Progress</h3>
        </div>
        <div id="progressContent">
            <div class="task-status pending">Pending...</div>
        </div>
    `;
    
    openModal('progressModal');
    
    // Poll progress
    const pollInterval = setInterval(async () => {
        try {
            const response = await fetch(`${API_BASE}/task/${taskId}/progress`);
            const data = await response.json();
            
            if (response.ok) {
                updateProgressDisplay(data);
                
                if (data.status === 'completed' || data.status === 'failed') {
                    clearInterval(pollInterval);
                    setTimeout(() => {
                        closeModal('progressModal');
                        loadPools();
                    }, 2000);
                }
            }
        } catch (error) {
            console.error('Progress poll error:', error);
        }
    }, POLL_INTERVAL);
}

function updateProgressDisplay(task) {
    const progressContent = document.getElementById('progressContent');
    
    progressContent.innerHTML = `
        <div class="task-status ${task.status}">${task.status.toUpperCase()}</div>
        <div class="progress-container">
            <div class="progress-text">
                ${task.current_operation || 'Processing...'}
                ${task.progress_percent}%
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: ${task.progress_percent}%"></div>
            </div>
            <div style="font-size: 12px; color: #7f8c8d; margin-top: 8px;">
                Elapsed: ${task.elapsed_seconds}s
                ${task.estimated_remaining_seconds ? ` | Estimated remaining: ${task.estimated_remaining_seconds}s` : ''}
            </div>
        </div>
        ${task.error ? `<div class="error-message">${task.error}</div>` : ''}
    `;
}

// ============ Helper Functions ============

async function loadPoolsForSelect(selectId) {
    try {
        const response = await fetch(`${API_BASE}/pools`);
        const data = await response.json();
        
        const select = document.getElementById(selectId);
        
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
        const response = await fetch(`${API_BASE}/pools`);
        const data = await response.json();
        
        const select = document.getElementById(selectId);
        
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

function closeModal(modalId) {
    document.getElementById(modalId).classList.remove('active');
}

function showError(message) {
    const errorDiv = document.createElement('div');
    errorDiv.className = 'error-message';
    errorDiv.textContent = message;
    
    const container = document.querySelector('.container');
    container.insertBefore(errorDiv, container.firstChild);
    
    setTimeout(() => {
        errorDiv.remove();
    }, 5000);
}

function showSuccess(message) {
    const successDiv = document.createElement('div');
    successDiv.className = 'success-message';
    successDiv.textContent = message;
    
    const container = document.querySelector('.container');
    container.insertBefore(successDiv, container.firstChild);
    
    setTimeout(() => {
        successDiv.remove();
    }, 5000);
}

// Close modals on background click
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.remove('active');
    }
});

// Restore session if available
window.addEventListener('load', () => {
    sessionId = localStorage.getItem('session_id');
    if (sessionId) {
        showDashboard();
    } else {
        showLoginScreen();
    }
});
