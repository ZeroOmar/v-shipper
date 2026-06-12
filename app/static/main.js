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
let taskPage = 0; // Current task list page (100 per page)
let consecutiveLoadFailures = 0; // Track network failures

// Configuration
const POLL_INTERVAL = 2000; // 2 seconds
const AUTO_REFRESH_INTERVAL = 30000; // 30 seconds
const MAX_TASK_HISTORY = 1000; // Keep last 1000 tasks
const TASKS_PER_PAGE = 100;
const MAX_LOAD_FAILURES = 3; // Logout after 3 consecutive failures

// ============ Utilities ============

function formatDate(ts) {
    if (!ts) return 'N/A';
    const d = new Date(ts * 1000);
    return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}/${d.getFullYear()}`;
}

function formatDateTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return `${String(d.getDate()).padStart(2,'0')}/${String(d.getMonth()+1).padStart(2,'0')}/${d.getFullYear()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

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
    switchMobileTab('pools');
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
        const response = await fetch(`${API_BASE}/pools`);
        
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
        loadTaskHistory();
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
            : 'Loading...';

        const isRemote = pool.pool_type === 'remote';
        const statsHtml = isRemote
            ? `<div class="stat"><div class="stat-label">Used</div><div class="stat-value">${pool.total_gb.toFixed(1)} GB</div></div>`
            : `<div class="stat"><div class="stat-label">Used</div><div class="stat-value">${pool.used_gb.toFixed(1)} GB</div></div>
               <div class="stat"><div class="stat-label">Free</div><div class="stat-value">${pool.available_gb.toFixed(1)} GB</div></div>`;
        const usageBarHtml = isRemote ? '' : `
            <div class="progress-bar" style="margin-top: 8px;">
                <div class="progress-fill" style="width: ${Math.min(usagePercent, 100)}%"></div>
            </div>`;
        const reachableHtml = pool.reachable === false
            ? `<div class="pool-unreachable">⚠ Unreachable</div>` : '';

        poolEl.innerHTML = `
            <div style="cursor: pointer;" onclick="selectPool('${pool.name}')">
                <div class="pool-name">${pool.name}</div>
                <div class="pool-type">${pool.role === 'backup' ? 'Backup' : 'Docker'} · ${pool.pool_type}</div>
                <div class="pool-counts">${countLabel}</div>
                ${reachableHtml}
                <div class="pool-stats" style="margin-top: 8px;">${statsHtml}</div>
                ${usageBarHtml}
            </div>
        `;
        
        container.appendChild(poolEl);
    });
}

function selectPool(poolName) {
    activePool = poolName;
    displayPools(Object.values(poolsCache));
    loadVolumesForPool(poolName);
    if (window.innerWidth <= 768) switchMobileTab('volumes');
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
        const response = await fetch(`${API_BASE}/volumes?pool=${poolName}`);
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
    const poolMeta = poolsCache[poolName] || {};
    const poolRole = poolMeta.role || 'docker';
    const isLocalDocker = poolRole !== 'backup' && poolMeta.pool_type !== 'remote';
    const container = document.getElementById('volumesContainer');

    // Backup pools get a grouped archive view
    if (poolRole === 'backup') {
        displayBackupPool(poolName, volumes, warnings);
        return;
    }

    let html = `<div class="volumes-header">
        <h2>${poolName}</h2>
        ${isLocalDocker ? `<button class="btn tonal" onclick="openCreateVolumeModal('${poolName}')">+ New Volume</button>` : ''}
    </div>`;
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
            const created = formatDate(volume.created_timestamp);
            const backupCount = volume.backups && volume.backups.length > 0 ? volume.backups.length : 0;
            const backupLabel = backupCount ? ` · ${backupCount} backup${backupCount !== 1 ? 's' : ''}` : '';

            html += `
                <div class="volume-item">
                    <div class="volume-info">
                        <div class="volume-name">${volume.name}</div>
                        <div class="volume-details">
                            <span class="volume-size">${sizeText}</span>
                            <span class="volume-meta">Created: ${created}${backupLabel}</span>
                        </div>
                    </div>
                    <div class="volume-actions">
                        <button class="btn vol-btn" onclick="openMigrateModal('${poolName}', '${volume.name}')">Migrate</button>
                        <button class="btn vol-btn" onclick="openBackupModal('${poolName}', '${volume.name}')">Backup</button>
                        ${isLocalDocker ? `<button class="btn tonal vol-btn" onclick="openRenameVolumeModal('${poolName}', '${volume.name}')">Rename</button>` : ''}
                        <button class="btn danger vol-btn" onclick="openDeleteModal('${poolName}', '${volume.name}')">Delete</button>
                    </div>
                </div>
            `;
        });
    }

    container.innerHTML = html;
}

// ── Backup Pool Grouped View ──────────────────────────────────────────────────

function parseBackupFilename(filename) {
    const m = filename.match(/^(.+)_(\d{8})_(\d{6})\.tar\.gz$/);
    if (!m) return null;
    const prefix = m[1], dateStr = m[2], timeStr = m[3];
    const dockerPools = Object.keys(poolsCache)
        .filter(k => poolsCache[k].role === 'docker')
        .sort((a, b) => b.length - a.length);
    let pool = null, volume = null;
    for (const p of dockerPools) {
        if (prefix === p) { pool = p; volume = ''; break; }
        if (prefix.startsWith(p + '_')) { pool = p; volume = prefix.slice(p.length + 1); break; }
    }
    if (pool === null) {
        const idx = prefix.indexOf('_');
        pool = idx === -1 ? prefix : prefix.slice(0, idx);
        volume = idx === -1 ? '' : prefix.slice(idx + 1);
    }
    const ts = new Date(`${dateStr.slice(0,4)}-${dateStr.slice(4,6)}-${dateStr.slice(6,8)}T${timeStr.slice(0,2)}:${timeStr.slice(2,4)}:${timeStr.slice(4,6)}`);
    return { pool, volume, timestamp: ts, label: volume || pool };
}

function displayBackupPool(poolName, archives, warnings = []) {
    const container = document.getElementById('volumesContainer');
    let html = `<div class="volumes-header"><h2>${poolName}</h2></div>`;
    if (warnings.length > 0) {
        html += `<div class="warning-banner">${warnings.map(w => `<div>${w}</div>`).join('')}</div>`;
    }
    if (archives.length === 0) {
        html += '<div class="placeholder"><p>No backups in this pool</p></div>';
        container.innerHTML = html;
        return;
    }

    const groups = {};
    const unparsed = [];
    archives.forEach(a => {
        const parsed = parseBackupFilename(a.name);
        if (!parsed) { unparsed.push(a); return; }
        const key = parsed.pool + '/' + (parsed.volume || '');
        if (!groups[key]) groups[key] = { label: parsed.volume || parsed.pool, pool: parsed.pool, items: [] };
        groups[key].items.push({ archive: a, parsed });
    });

    // Sort groups alphabetically by label
    const sortedGroups = Object.entries(groups).sort(([, a], [, b]) => a.label.localeCompare(b.label));

    sortedGroups.forEach(([, g]) => {
        g.items.sort((a, b) => b.parsed.timestamp - a.parsed.timestamp);
        const count = g.items.length;
        html += `<div class="backup-group">
            <div class="backup-group-header">${escapeHtml(g.label)} <span>from ${escapeHtml(g.pool)} · ${count} backup${count !== 1 ? 's' : ''}</span></div>
            <div class="backup-group-items">`;
        g.items.forEach(({ archive: a, parsed }) => {
            const size = formatVolumeSize(a.size_bytes, a.size_gb);
            const dt = `${String(parsed.timestamp.getDate()).padStart(2,'0')}/${String(parsed.timestamp.getMonth()+1).padStart(2,'0')}/${parsed.timestamp.getFullYear()} ${String(parsed.timestamp.getHours()).padStart(2,'0')}:${String(parsed.timestamp.getMinutes()).padStart(2,'0')}`;
            html += `<div class="backup-item">
                <div>
                    <div class="backup-item-name">${escapeHtml(a.name)}</div>
                    <div class="backup-item-meta">${dt} · ${size}</div>
                </div>
                <div style="display:flex;gap:6px;">
                    <button class="btn vol-btn" onclick="openRestoreModal('${poolName}', '${a.name}')">Restore</button>
                    <button class="btn danger vol-btn" onclick="openDeleteModal('${poolName}', '${a.name}')">Delete</button>
                </div>
            </div>`;
        });
        html += `</div></div>`;
    });

    if (unparsed.length > 0) {
        html += `<div class="backup-group">
            <div class="backup-group-header">Other <span>${unparsed.length} file${unparsed.length !== 1 ? 's' : ''}</span></div>
            <div class="backup-group-items">`;
        unparsed.forEach(a => {
            const size = formatVolumeSize(a.size_bytes, a.size_gb);
            html += `<div class="backup-item">
                <div>
                    <div class="backup-item-name">${escapeHtml(a.name)}</div>
                    <div class="backup-item-meta">${size}</div>
                </div>
                <div style="display:flex;gap:6px;">
                    <button class="btn vol-btn" onclick="openRestoreModal('${poolName}', '${a.name}')">Restore</button>
                    <button class="btn danger vol-btn" onclick="openDeleteModal('${poolName}', '${a.name}')">Delete</button>
                </div>
            </div>`;
        });
        html += `</div></div>`;
    }

    container.innerHTML = html;
}

// ── Create / Rename Volume ─────────────────────────────────────────────────────

function openCreateVolumeModal(poolName) {
    const modal = document.getElementById('migrateModal');
    modal.querySelector('.modal-content').innerHTML = `
        <div class="modal-header">
            <h3>New Volume</h3>
            <button class="close-btn" onclick="closeModal('migrateModal')">×</button>
        </div>
        <div class="modal-body">
            <div class="form-group">
                <label>Pool</label>
                <input type="text" value="${escapeHtml(poolName)}" disabled>
            </div>
            <div class="form-group">
                <label>Volume Name</label>
                <input type="text" id="newVolumeName" placeholder="my-volume" autofocus>
            </div>
            <button class="btn success" style="width:100%;" onclick="createVolume('${poolName}')">Create</button>
        </div>`;
    openModal('migrateModal');
    document.getElementById('newVolumeName')?.focus();
}

async function createVolume(poolName) {
    const name = document.getElementById('newVolumeName')?.value.trim();
    if (!name) { showError('Volume name is required'); return; }
    try {
        const res = await fetch(`${API_BASE}/volume/create`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pool: poolName, volume_name: name }),
        });
        const data = await res.json();
        if (res.ok) {
            closeModal('migrateModal');
            showSuccess(`Volume '${name}' created`);
            loadVolumesForPool(poolName);
        } else {
            showError(data.detail || 'Failed to create volume');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

function openRenameVolumeModal(poolName, volumeName) {
    const modal = document.getElementById('migrateModal');
    modal.querySelector('.modal-content').innerHTML = `
        <div class="modal-header">
            <h3>Rename Volume</h3>
            <button class="close-btn" onclick="closeModal('migrateModal')">×</button>
        </div>
        <div class="modal-body">
            <div class="form-group">
                <label>Pool</label>
                <input type="text" value="${escapeHtml(poolName)}" disabled>
            </div>
            <div class="form-group">
                <label>Current Name</label>
                <input type="text" value="${escapeHtml(volumeName)}" disabled>
            </div>
            <div class="form-group">
                <label>New Name</label>
                <input type="text" id="renameVolumeName" value="${escapeHtml(volumeName)}" autofocus>
            </div>
            <button class="btn success" style="width:100%;" onclick="renameVolume('${poolName}', '${volumeName}')">Rename</button>
        </div>`;
    openModal('migrateModal');
    const inp = document.getElementById('renameVolumeName');
    if (inp) { inp.focus(); inp.select(); }
}

async function renameVolume(poolName, oldName) {
    const newName = document.getElementById('renameVolumeName')?.value.trim();
    if (!newName) { showError('New name is required'); return; }
    if (newName === oldName) { closeModal('migrateModal'); return; }
    try {
        const res = await fetch(`${API_BASE}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pool: poolName, old_name: oldName, new_name: newName }),
        });
        const data = await res.json();
        if (res.ok) {
            closeModal('migrateModal');
            showSuccess(`Renamed '${oldName}' → '${newName}'`);
            loadVolumesForPool(poolName);
        } else {
            showError(data.detail || 'Failed to rename volume');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
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
            const response = await fetch(`${API_BASE}/volumes?pool=${poolName}`);
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

    await _doMigrate({
        source_pool: sourcePool,
        source_volume: sourceVolume,
        dest_pool: destPool,
        verify,
        delete_source: deleteSource,
    });
}

async function _doMigrate(params) {
    try {
        const response = await fetch(`${API_BASE}/migrate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params),
        });

        if (response.status === 409) {
            const body = await response.json();
            const d = body.detail;
            if (d && d.code === 'destination_exists') {
                closeModal('migrateModal');
                _showConflictModal(d.dest_volume, d.dest_pool, (resolution, renameVal) => {
                    _doMigrate({ ...params, conflict_resolution: resolution, rename_dest: renameVal || undefined });
                });
                return;
            }
        }

        const data = await response.json();
        if (response.ok) {
            closeModal('migrateModal');
            showTaskProgress(data.task_id);
        } else {
            showError(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
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
    const _stripped = backupFile.replace(/\.tar\.gz$/, '').replace(/_\d{8}_\d{6}$/, '');
    const defaultVolume = _stripped.includes('_') ? _stripped.slice(_stripped.indexOf('_') + 1) : _stripped;

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

    await _doRestore({
        backup_pool: backupPool,
        backup_file: backupFile,
        dest_pool: destPool,
        dest_volume: destVolume,
    });
}

async function _doRestore(params) {
    try {
        const response = await fetch(`${API_BASE}/restore`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params),
        });

        if (response.status === 409) {
            const body = await response.json();
            const d = body.detail;
            if (d && d.code === 'destination_exists') {
                closeModal('backupModal');
                _showConflictModal(d.dest_volume, d.dest_pool, (resolution, renameVal) => {
                    _doRestore({
                        ...params,
                        conflict_resolution: resolution,
                        rename_dest: renameVal || undefined,
                        // For restore, when renaming update dest_volume too
                        ...(resolution === 'rename' && renameVal ? { dest_volume: renameVal } : {}),
                    });
                });
                return;
            }
        }

        const data = await response.json();
        if (response.ok) {
            closeModal('backupModal');
            showTaskProgress(data.task_id);
        } else {
            showError(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
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
        current_operation: task.current_operation || null,
        progress_percent: task.progress_percent || 0,
        status: task.status || 'pending',
        elapsed_seconds: task.elapsed_seconds || 0,
        estimated_remaining_seconds: task.estimated_remaining_seconds || null,
        error: task.error || null,
        params: task.params || {},
        started_at: task.started_at || null,
        completed_at: task.completed_at || null,
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
    if (task.task_type === 'scheduled_backup') {
        const name = params.job_name || 'Scheduled Backup';
        const count = params.total_volumes || 0;
        const pool = params.backup_pool || '';
        return `${name} — ${count} volume${count !== 1 ? 's' : ''}${pool ? ` → ${pool}` : ''}`;
    }
    if (task.task_type === 'delete') {
        const pool = params.pool || '';
        const vol = params.volume_name || params.source_volume || params.volume || '';
        return pool && vol ? `${pool}/${vol}` : (vol || 'Delete operation');
    }
    if (task.task_type === 'backup') {
        if (params.source_volume) {
            const src = params.source_pool ? `${params.source_pool}/` : '';
            return `${src}${params.source_volume} → ${params.backup_pool || 'backup pool'}`;
        }
        return 'Backup task';
    }
    if (task.task_type === 'migrate') {
        if (params.source_volume) {
            return `${params.source_pool || '?'}/${params.source_volume} → ${params.dest_pool || '?'}`;
        }
        return 'Migration task';
    }
    if (task.task_type === 'restore') {
        const file = params.backup_file || params.source_volume;
        if (file) {
            const src = params.backup_pool ? `${params.backup_pool}/` : '';
            const dstVol = params.dest_volume_name || params.dest_volume || '';
            const dst = params.dest_pool ? `${params.dest_pool}/${dstVol}` : dstVol || 'destination';
            return `${src}${file} → ${dst}`;
        }
        return 'Restore task';
    }
    return params.source_volume || params.volume_name || params.backup_file || task.current_operation || 'Task';
}

function renderTaskHistory() {
    const progressPanel = document.getElementById('progressPanel');

    if (taskHistory.length === 0) {
        progressPanel.innerHTML = '<div class="placeholder-text">No active tasks</div>';
        return;
    }

    const totalPages = Math.ceil(taskHistory.length / TASKS_PER_PAGE);
    taskPage = Math.max(0, Math.min(taskPage, totalPages - 1));
    const pageTasks = taskHistory.slice(taskPage * TASKS_PER_PAGE, (taskPage + 1) * TASKS_PER_PAGE);

    const items = pageTasks.map(task => {
        const targetLabel = getTaskTargetLabel(task);
        const typeClass = (task.task_type || 'operation').replace(/[^a-z0-9_]/gi, '_');
        return `
            <div class="task-history-item" onclick="openTaskDetailModalById('${task.task_id}')" title="Click for details">
                <div class="task-card-pills">
                    <div class="task-type-pill type-${typeClass}">${getTaskTypeDisplay(task.task_type)}</div>
                    <div class="task-status ${task.status}">${task.status.toUpperCase()}</div>
                </div>
                <div class="progress-text task-target">${targetLabel}</div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${task.progress_percent}%"></div>
                </div>
                <div class="task-meta">${task.progress_percent}% · ${task.elapsed_seconds}s${task.estimated_remaining_seconds ? ` · ${task.estimated_remaining_seconds}s left` : ''}</div>
            </div>
        `;
    }).join('');

    const pagination = totalPages > 1 ? `
        <div class="task-pagination">
            <button class="btn tonal task-page-btn" onclick="setTaskPage(${taskPage - 1})" ${taskPage === 0 ? 'disabled' : ''}>‹</button>
            <span class="task-page-label">${taskPage + 1} / ${totalPages}</span>
            <button class="btn tonal task-page-btn" onclick="setTaskPage(${taskPage + 1})" ${taskPage >= totalPages - 1 ? 'disabled' : ''}>›</button>
        </div>` : '';

    progressPanel.innerHTML = items + pagination;
}

function setTaskPage(page) {
    taskPage = page;
    renderTaskHistory();
}

function getTaskTypeDisplay(type) {
    const map = {
        backup: 'Backup',
        scheduled_backup: 'Scheduled',
        migrate: 'Migrate',
        restore: 'Restore',
        delete: 'Delete',
        rename: 'Rename',
        create: 'Create',
    };
    return map[type] || (type ? type.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : 'Task');
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
        const response = await fetch(`${API_BASE}/tasks`);
        const data = await response.json();

        if (!response.ok) {
            return;
        }

        taskHistory = data.tasks.map(task => ({
            task_id: task.task_id,
            task_type: task.task_type || 'operation',
            current_operation: task.current_operation || null,
            progress_percent: task.progress_percent || 0,
            status: task.status || 'pending',
            elapsed_seconds: task.elapsed_seconds || 0,
            estimated_remaining_seconds: task.estimated_remaining_seconds || null,
            error: task.error || null,
            params: task.params || {},
            started_at: task.started_at || null,
            completed_at: task.completed_at || null,
        }));
        taskHistory = taskHistory.slice(0, MAX_TASK_HISTORY);
        renderTaskHistory();
    } catch (error) {
        console.warn('Failed to load task history:', error.message);
    }
}

// ============ Helper Functions ============

function poolSizeLabel(pool) {
    if (pool.pool_type === 'remote') return `remote · ${pool.total_gb.toFixed(1)} GB used`;
    return `${pool.available_gb.toFixed(1)} GB free`;
}

function loadPoolsForSelect(selectId) {
    const select = document.getElementById(selectId);
    select.innerHTML = '<option value="">-- Select pool --</option>';
    Object.values(poolsCache).forEach(pool => {
        if (pool.role !== 'backup') {
            const option = document.createElement('option');
            option.value = pool.name;
            option.textContent = `${pool.name} (${poolSizeLabel(pool)})`;
            select.appendChild(option);
        }
    });
}

function loadBackupPoolsForSelect(selectId) {
    const select = document.getElementById(selectId);
    select.innerHTML = '<option value="">-- Select backup pool --</option>';
    Object.values(poolsCache).forEach(pool => {
        if (pool.role === 'backup') {
            const option = document.createElement('option');
            option.value = pool.name;
            option.textContent = `${pool.name} (${poolSizeLabel(pool)})`;
            select.appendChild(option);
        }
    });
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

// ── Settings Overlay ──────────────────────────────────────────────────────────

function openSettings() {
    document.getElementById('settingsOverlay').classList.add('active');
    showSettingsSection('appearance');
}

function closeSettings() {
    document.getElementById('settingsOverlay').classList.remove('active');
}

function switchMobileTab(tab) {
    document.querySelectorAll('.mobile-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    const map = { pools: '.pools-sidebar', volumes: '.volumes-section', tasks: '.progress-sidebar' };
    Object.entries(map).forEach(([key, sel]) => {
        document.querySelector(sel).classList.toggle('mobile-active', key === tab);
    });
}

function showSettingsSection(name) {
    document.querySelectorAll('.settings-nav-item').forEach(el => {
        el.classList.toggle('active', el.dataset.section === name);
    });
    const content = document.getElementById('settingsContent');
    if (name === 'appearance') {
        const current = localStorage.getItem(THEME_KEY) || 'system';
        content.innerHTML = `
            <h2 class="settings-section-title">Appearance</h2>
            <p class="settings-section-desc">Choose your preferred color theme.</p>
            <div class="theme-options">
                ${['system','light','dark'].map(t => `
                <div class="theme-card ${current === t ? 'active' : ''}" onclick="selectTheme('${t}')">
                    <div class="theme-card-icon">${t === 'system' ? '💻' : t === 'light' ? '☀️' : '🌙'}</div>
                    <div class="theme-card-label">${t.charAt(0).toUpperCase() + t.slice(1)}</div>
                </div>`).join('')}
            </div>`;
    } else if (name === 'maintenance') {
        content.innerHTML = `
            <h2 class="settings-section-title">Maintenance</h2>
            <p class="settings-section-desc">Remove stale lock files and the persisted task state file. Use this if tasks appear stuck or history is out of sync.</p>
            <button class="btn tonal" onclick="runTroubleshootCleanup()">🧹 Run Cleanup</button>`;
    } else if (name === 'about') {
        content.innerHTML = `
            <h2 class="settings-section-title">v-shipper</h2>
            <p class="settings-section-desc">Docker volume migration and backup tool.</p>
            <p class="settings-about-version">Version: <strong id="aboutVersion">–</strong></p>
            <a href="https://github.com/ZeroOmar/v-shipper" target="_blank" rel="noopener" class="btn tonal">GitHub →</a>`;
        fetch(`${API_BASE}/health`).then(r => r.json()).then(d => {
            const el = document.getElementById('aboutVersion');
            if (el) el.textContent = d.version || '–';
        }).catch(() => {});
    } else if (name === 'schedules') {
        content.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                <h2 class="settings-section-title" style="margin:0;">Backup Schedules</h2>
                <button class="btn" onclick="openScheduleForm()">+ New Schedule</button>
            </div>
            <p class="settings-section-desc">Automated recurring backups with retention policies.</p>
            <div id="scheduleList" class="schedule-list"><div class="placeholder-text">Loading…</div></div>`;
        loadSchedules();
    }
}

function selectTheme(theme) {
    localStorage.setItem(THEME_KEY, theme);
    if (theme === 'system') {
        const darkMode = window.matchMedia('(prefers-color-scheme: dark)').matches;
        applyTheme(darkMode ? 'dark' : 'light');
    } else {
        applyTheme(theme);
    }
    showSettingsSection('appearance');
}

// ── Backup Schedules ──────────────────────────────────────────────────────────

async function loadSchedules() {
    const list = document.getElementById('scheduleList');
    if (!list) return;
    try {
        const res = await fetch(`${API_BASE}/schedules`, { headers: { 'Authorization': `Bearer ${sessionId}` } });
        const data = await res.json();
        if (res.ok) {
            renderScheduleList(data.schedules || []);
        } else {
            list.innerHTML = `<div class="placeholder-text">Failed to load schedules</div>`;
        }
    } catch (e) {
        if (list) list.innerHTML = `<div class="placeholder-text">Error: ${escapeHtml(e.message)}</div>`;
    }
}

function renderScheduleList(schedules) {
    const list = document.getElementById('scheduleList');
    if (!list) return;
    if (!schedules.length) {
        list.innerHTML = `<div class="placeholder-text">No schedules yet. Click "+ New Schedule" to create one.</div>`;
        return;
    }
    list.innerHTML = schedules.map(job => {
        const nextRun = job.next_run ? new Date(job.next_run * 1000).toLocaleString() : 'N/A';
        const volCount = (job.volumes || []).length;
        const enabledClass = job.enabled ? 'on' : 'off';
        const enabledLabel = job.enabled ? 'Enabled' : 'Disabled';
        return `
        <div class="schedule-row">
            <div class="schedule-row-main">
                <div class="schedule-row-name">${escapeHtml(job.name)}</div>
                <div class="schedule-row-meta">
                    <code>${escapeHtml(job.cron)}</code> &nbsp;·&nbsp;
                    Pool: <strong>${escapeHtml(job.backup_pool)}</strong> &nbsp;·&nbsp;
                    ${volCount} volume${volCount !== 1 ? 's' : ''} &nbsp;·&nbsp;
                    Keep ${job.retention} backup${job.retention !== 1 ? 's' : ''}
                </div>
                <div class="schedule-next-run">Next run: ${nextRun}</div>
            </div>
            <div class="schedule-row-actions">
                <span class="schedule-enabled-chip ${enabledClass}" onclick="toggleSchedule('${job.id}')">${enabledLabel}</span>
                <button class="btn tonal" onclick="runScheduleNow('${job.id}')">▶ Run</button>
                <button class="btn tonal" onclick="openScheduleForm('${job.id}')">Edit</button>
                <button class="btn danger" onclick="deleteSchedule('${job.id}')">Delete</button>
            </div>
        </div>`;
    }).join('');
}

async function openScheduleForm(jobId = null) {
    const content = document.getElementById('settingsContent');
    content.innerHTML = `<div class="placeholder-text">Loading…</div>`;

    let job = null;
    if (jobId) {
        try {
            const res = await fetch(`${API_BASE}/schedules`, { headers: { 'Authorization': `Bearer ${sessionId}` } });
            const data = await res.json();
            job = (data.schedules || []).find(j => j.id === jobId) || null;
        } catch (e) { /* ignore */ }
    }

    // Fetch all pools to populate backup pool dropdown and volume checkboxes
    let pools = [];
    let dockerPools = [];
    let volumesByPool = {};
    try {
        const res = await fetch(`${API_BASE}/pools`, { headers: { 'Authorization': `Bearer ${sessionId}` } });
        const data = await res.json();
        pools = data.pools || [];
        dockerPools = pools.filter(p => p.role === 'docker');
        // fetch volumes for each docker pool in parallel
        await Promise.all(dockerPools.map(async p => {
            try {
                const vRes = await fetch(`${API_BASE}/volumes?pool=${encodeURIComponent(p.name)}`, { headers: { 'Authorization': `Bearer ${sessionId}` } });
                const vData = await vRes.json();
                volumesByPool[p.name] = (vData.volumes || []).map(v => v.name);
            } catch (e) { volumesByPool[p.name] = []; }
        }));
    } catch (e) { /* ignore */ }

    const backupPools = pools.filter(p => p.role === 'backup');
    const selectedVols = new Set((job?.volumes || []).map(v => `${v.pool}::${v.volume}`));

    const cronExamples = [
        { label: 'Daily 2am', value: '0 2 * * *' },
        { label: 'Every 6h', value: '0 */6 * * *' },
        { label: 'Weekly Sun', value: '0 2 * * 0' },
        { label: 'Monthly 1st', value: '0 2 1 * *' },
    ];

    const volumeGroupsHtml = dockerPools.map(p => {
        const vols = volumesByPool[p.name] || [];
        if (!vols.length) return '';
        return `<div>
            <div class="schedule-pool-group-title">${escapeHtml(p.name)}</div>
            ${vols.map(v => {
                const key = `${p.name}::${v}`;
                const checked = selectedVols.has(key) ? 'checked' : '';
                return `<label class="schedule-vol-item">
                    <input type="checkbox" data-pool="${escapeHtml(p.name)}" data-vol="${escapeHtml(v)}" ${checked}>
                    ${escapeHtml(v)}
                </label>`;
            }).join('')}
        </div>`;
    }).join('');

    content.innerHTML = `
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px;">
            <button class="btn tonal" onclick="showSettingsSection('schedules')">← Back</button>
            <h2 class="settings-section-title" style="margin:0;">${jobId ? 'Edit Schedule' : 'New Schedule'}</h2>
        </div>
        <div class="schedule-form">
            <label>Name
                <input type="text" id="sfName" value="${escapeHtml(job?.name || '')}" placeholder="e.g. Nightly Production Backup">
            </label>
            <label>Cron Expression
                <input type="text" id="sfCron" value="${escapeHtml(job?.cron || '0 2 * * *')}" placeholder="0 2 * * *">
                <div class="cron-examples">
                    ${cronExamples.map(e => `<span class="cron-chip" onclick="document.getElementById('sfCron').value='${e.value}'">${e.label} <code>${e.value}</code></span>`).join('')}
                </div>
            </label>
            <label>Backup Pool
                <select id="sfBackupPool">
                    ${backupPools.map(p => `<option value="${escapeHtml(p.name)}" ${job?.backup_pool === p.name ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
                    ${!backupPools.length ? '<option value="" disabled>No backup pools configured</option>' : ''}
                </select>
            </label>
            <label>Retention (backups to keep per volume)
                <input type="number" id="sfRetention" value="${job?.retention ?? 7}" min="1" max="365">
            </label>
            <label>Volumes to Back Up
                <div class="schedule-volume-groups" id="sfVolumeGroups">
                    ${volumeGroupsHtml || '<div class="placeholder-text">No volumes available</div>'}
                </div>
            </label>
            <div class="schedule-form-actions">
                <button class="btn success" onclick="saveSchedule(${jobId ? `'${jobId}'` : 'null'})">Save</button>
                <button class="btn tonal" onclick="showSettingsSection('schedules')">Cancel</button>
            </div>
        </div>`;
}

async function saveSchedule(jobId) {
    const name = document.getElementById('sfName')?.value.trim();
    const cron = document.getElementById('sfCron')?.value.trim();
    const backupPool = document.getElementById('sfBackupPool')?.value;
    const retention = parseInt(document.getElementById('sfRetention')?.value, 10) || 7;

    if (!name) { showError('Schedule name is required'); return; }
    if (!cron) { showError('Cron expression is required'); return; }
    if (!backupPool) { showError('Backup pool is required'); return; }

    const volumes = [];
    document.querySelectorAll('#sfVolumeGroups input[type="checkbox"]:checked').forEach(cb => {
        volumes.push({ pool: cb.dataset.pool, volume: cb.dataset.vol });
    });
    if (!volumes.length) { showError('Select at least one volume'); return; }

    const body = { name, cron, backup_pool: backupPool, volumes, retention };
    const url = jobId ? `${API_BASE}/schedules/${jobId}` : `${API_BASE}/schedules`;
    const method = jobId ? 'PUT' : 'POST';

    try {
        const res = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${sessionId}` },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (res.ok) {
            showSuccess(jobId ? 'Schedule updated' : 'Schedule created');
            showSettingsSection('schedules');
        } else {
            showError(data.detail || 'Failed to save schedule');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

async function deleteSchedule(jobId) {
    if (!confirm('Delete this backup schedule? This cannot be undone.')) return;
    try {
        const res = await fetch(`${API_BASE}/schedules/${jobId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${sessionId}` },
        });
        if (res.ok) {
            showSuccess('Schedule deleted');
            loadSchedules();
        } else {
            const data = await res.json();
            showError(data.detail || 'Failed to delete schedule');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

async function toggleSchedule(jobId) {
    try {
        const res = await fetch(`${API_BASE}/schedules/${jobId}/toggle`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${sessionId}` },
        });
        if (res.ok) {
            loadSchedules();
        } else {
            const data = await res.json();
            showError(data.detail || 'Failed to toggle schedule');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

async function runScheduleNow(jobId) {
    try {
        const res = await fetch(`${API_BASE}/schedules/${jobId}/run`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${sessionId}` },
        });
        if (res.ok) {
            showSuccess('Backup job triggered — check the Tasks panel');
        } else {
            const data = await res.json();
            showError(data.detail || 'Failed to trigger schedule');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

// ── Conflict Resolution Modal ─────────────────────────────────────────────────

let _conflictCallback = null;

function _showConflictModal(destVolume, destPool, onProceed) {
    _conflictCallback = onProceed;

    document.getElementById('conflictModalBody').innerHTML = `
        <p class="conflict-desc">
            <strong>${escapeHtml(destVolume)}</strong> already exists in <strong>${escapeHtml(destPool)}</strong>.
            Choose how to proceed:
        </p>
        <div class="conflict-options">
            <div class="conflict-option" data-res="overwrite" onclick="selectConflictOption(this)">
                <div class="conflict-option-content">
                    <div class="conflict-option-title">Overwrite target</div>
                    <div class="conflict-option-desc">Completely replace the existing volume (adds, updates, and deletes files)</div>
                </div>
            </div>
            <div class="conflict-option" data-res="merge" onclick="selectConflictOption(this)">
                <div class="conflict-option-content">
                    <div class="conflict-option-title">Merge into target</div>
                    <div class="conflict-option-desc">Add new files and update existing ones; keep files unique to the destination</div>
                </div>
            </div>
            <div class="conflict-option" data-res="rename" onclick="selectConflictOption(this)">
                <div class="conflict-option-content">
                    <div class="conflict-option-title">Rename destination</div>
                    <div class="conflict-option-desc">Save to a different volume name instead</div>
                </div>
                <div class="conflict-rename-wrap" id="conflictRenameWrap">
                    <input type="text" id="conflictRenameInput" class="conflict-rename-input"
                        placeholder="New volume name"
                        oninput="_validateConflictProceed()"
                        onclick="event.stopPropagation()">
                </div>
            </div>
        </div>
        <div class="conflict-actions">
            <button class="btn tonal" onclick="abortConflict()">Abort</button>
            <button class="btn success" id="conflictProceedBtn" onclick="proceedConflict()" disabled>Proceed</button>
        </div>
    `;
    openModal('conflictModal');
}

function selectConflictOption(el) {
    document.querySelectorAll('.conflict-option').forEach(o => o.classList.remove('selected'));
    el.classList.add('selected');
    const isRename = el.dataset.res === 'rename';
    document.getElementById('conflictRenameWrap').classList.toggle('visible', isRename);
    if (isRename) document.getElementById('conflictRenameInput').focus();
    _validateConflictProceed();
}

function _validateConflictProceed() {
    const selected = document.querySelector('.conflict-option.selected');
    const btn = document.getElementById('conflictProceedBtn');
    if (!btn) return;
    if (!selected) { btn.disabled = true; return; }
    if (selected.dataset.res === 'rename') {
        const val = (document.getElementById('conflictRenameInput')?.value || '').trim();
        btn.disabled = !val;
    } else {
        btn.disabled = false;
    }
}

function proceedConflict() {
    const selected = document.querySelector('.conflict-option.selected');
    if (!selected) return;
    const resolution = selected.dataset.res;
    let renameVal = null;
    if (resolution === 'rename') {
        renameVal = (document.getElementById('conflictRenameInput')?.value || '').trim();
        if (!renameVal) { showError('Please enter a new volume name'); return; }
    }
    closeModal('conflictModal');
    if (_conflictCallback) {
        const cb = _conflictCallback;
        _conflictCallback = null;
        cb(resolution, renameVal);
    }
}

function abortConflict() {
    _conflictCallback = null;
    closeModal('conflictModal');
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ── Task Detail Modal ──────────────────────────────────────────────────────────

let _taskDetailPollInterval = null;
let _taskDetailCurrentId = null;

const PARAM_LABELS = {
    source_pool:       'Source Pool',
    source_volume:     'Source Volume',
    dest_pool:         'Destination Pool',
    dest_volume:       'Destination Volume',
    dest_volume_name:  'Dest Volume Name',
    backup_pool:       'Backup Pool',
    backup_file:       'Backup File',
    pool:              'Pool',
    volume_name:       'Volume',
    verify:            'Verify',
    delete_source:     'Delete Source',
    compress:          'Compress',
    exclude_patterns:  'Exclude Patterns',
};

function openTaskDetailModalById(taskId) {
    const task = taskHistory.find(t => t.task_id === taskId);
    if (task) openTaskDetailModal(task);
}

function openTaskDetailModal(task) {
    _taskDetailCurrentId = task.task_id;
    document.getElementById('taskDetailTitle').textContent =
        (task.task_type || 'Task').toUpperCase();
    document.getElementById('taskDetailBody').innerHTML =
        '<div class="task-detail-loading">Loading…</div>';
    openModal('taskDetailModal');
    _refreshTaskDetail(task.task_id);
    if (task.status === 'running' || task.status === 'pending') {
        _taskDetailPollInterval = setInterval(() => _refreshTaskDetail(_taskDetailCurrentId), 2000);
    }
}

function closeTaskDetail() {
    if (_taskDetailPollInterval) {
        clearInterval(_taskDetailPollInterval);
        _taskDetailPollInterval = null;
    }
    _taskDetailCurrentId = null;
    closeModal('taskDetailModal');
}

async function _refreshTaskDetail(taskId) {
    if (!taskId) return;
    try {
        const [progRes, logsRes] = await Promise.all([
            fetch(`${API_BASE}/task/${taskId}/progress`),
            fetch(`${API_BASE}/task/${taskId}/logs`),
        ]);
        const [prog, logs] = await Promise.all([
            progRes.ok  ? progRes.json()  : null,
            logsRes.ok  ? logsRes.json()  : { lines: [] },
        ]);

        if (prog) {
            // Keep taskHistory in sync
            const idx = taskHistory.findIndex(t => t.task_id === taskId);
            if (idx >= 0) taskHistory[idx] = { ...taskHistory[idx], ...prog };
            _renderTaskDetail(prog, logs.lines || []);

            if (prog.status === 'completed' || prog.status === 'failed') {
                if (_taskDetailPollInterval) {
                    clearInterval(_taskDetailPollInterval);
                    _taskDetailPollInterval = null;
                }
            }
        }
    } catch (e) { /* network error — silently skip */ }
}

function _renderTaskDetail(task, logLines) {
    if (_taskDetailCurrentId !== task.task_id) return;

    document.getElementById('taskDetailTitle').textContent =
        (task.task_type || 'Task').toUpperCase();

    const params = task.params || {};
    const paramRows = Object.entries(params)
        .filter(([, v]) => v !== null && v !== undefined && v !== '')
        .map(([k, v]) => {
            const label = PARAM_LABELS[k] || k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
            let val;
            if (Array.isArray(v)) val = `<code>${escapeHtml(JSON.stringify(v))}</code>`;
            else if (typeof v === 'boolean') val = v ? 'Yes' : 'No';
            else if (typeof v === 'object') val = `<code>${escapeHtml(JSON.stringify(v))}</code>`;
            else val = escapeHtml(String(v));
            return `<tr><td class="param-key">${label}</td><td class="param-val">${val}</td></tr>`;
        }).join('');

    const logHtml = logLines.length
        ? logLines.map(l => `<div class="log-line">${escapeHtml(l)}</div>`).join('')
        : '<div class="log-empty">No logs captured yet.</div>';

    const startedStr  = formatDateTime(task.started_at);
    const completedStr = formatDateTime(task.completed_at);

    document.getElementById('taskDetailBody').innerHTML = `
        <div class="task-detail-header">
            <span class="task-type-pill type-${(task.task_type || 'operation').replace(/[^a-z0-9_]/gi, '_')}">${getTaskTypeDisplay(task.task_type)}</span>
            <span class="task-status ${task.status}">${task.status.toUpperCase()}</span>
            <span class="task-detail-target">${escapeHtml(getTaskTargetLabel(task))}</span>
        </div>

        <div class="task-detail-progress">
            <div class="progress-bar">
                <div class="progress-fill" style="width: ${task.progress_percent || 0}%"></div>
            </div>
            <div class="task-detail-stats">
                <span>${task.progress_percent || 0}%</span>
                <span>${task.elapsed_seconds || 0}s elapsed</span>
                ${task.estimated_remaining_seconds ? `<span>~${task.estimated_remaining_seconds}s left</span>` : ''}
                <span>Started: ${startedStr}</span>
                ${task.completed_at ? `<span>Finished: ${completedStr}</span>` : ''}
            </div>
        </div>

        ${task.current_operation ? `<div class="task-detail-op">${escapeHtml(task.current_operation)}</div>` : ''}
        ${task.error ? `<div class="error-message task-detail-error">${escapeHtml(task.error).replace(/\n/g, '<br>')}</div>` : ''}

        ${paramRows ? `
        <div class="task-detail-section">
            <div class="task-detail-section-title">Parameters</div>
            <table class="param-table"><tbody>${paramRows}</tbody></table>
        </div>` : ''}

        <div class="task-detail-section">
            <div class="task-detail-section-title">
                Logs
                ${(_taskDetailPollInterval) ? '<span class="log-live-badge">● LIVE</span>' : ''}
            </div>
            <div class="task-log-terminal" id="taskLogTerminal">${logHtml}</div>
        </div>
    `;

    // Auto-scroll log terminal to bottom
    const term = document.getElementById('taskLogTerminal');
    if (term) term.scrollTop = term.scrollHeight;
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

// Close modals / settings on background click
document.addEventListener('click', (e) => {
    if (e.target.id === 'settingsOverlay') {
        closeSettings();
    } else if (e.target.classList.contains('modal')) {
        if (e.target.id === 'taskDetailModal') {
            closeTaskDetail();
        } else if (e.target.id === 'conflictModal') {
            abortConflict();
        } else {
            e.target.classList.remove('active');
        }
    }
});

// Restore session if available
