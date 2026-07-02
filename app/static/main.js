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
let appVersion = null; // v-shipper's own version, fetched once from /api/health
let latestShipperVersion = null; // highest v-shipper tag on GitHub (null until checked)
let latestHelperVersion = null;  // highest v-helper tag on GitHub (null until checked)
let versionCheckInterval = null; // periodic GitHub latest-version poll
let containerUsageCache = {}; // pool -> { volume: [{name, status}] }, from /api/containers
let bulkSelection = new Set();   // names of volumes/backups selected for a bulk action
let bulkSelectionPool = null;    // pool the current selection belongs to (reset on pool change)

// Configuration
const POLL_INTERVAL = 2000; // 2 seconds
const AUTO_REFRESH_INTERVAL = 30000; // 30 seconds
const MAX_TASK_HISTORY = 1000; // Keep last 1000 tasks
const TASKS_PER_PAGE = 100;

// Volume/pool name rules — mirror app/validation.py NAME_RE (server is authoritative).
const VOLUME_NAME_PATTERN = '[A-Za-z0-9][A-Za-z0-9_.-]{0,254}';
const VOLUME_NAME_TITLE = 'Letters, digits, "_", ".", "-"; must start with a letter or digit; no spaces or slashes.';
const MAX_LOAD_FAILURES = 3; // Logout after 3 consecutive failures

// GitHub tag-list endpoints for the two components. The browser fetches these
// directly (GitHub's API sends permissive CORS headers for public repos) to
// learn each component's latest released version, independently of the other.
const SHIPPER_TAGS_URL = 'https://api.github.com/repos/ZeroOmar/v-shipper/tags';
const HELPER_TAGS_URL = 'https://api.github.com/repos/ZeroOmar/v-helper/tags';
const VERSION_CHECK_INTERVAL = 6 * 60 * 60 * 1000; // re-check GitHub every 6 hours

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

// Format a duration given in seconds as hh:mm:ss.SSS
function formatDuration(seconds) {
    const total = Number(seconds);
    if (!isFinite(total) || total < 0) return '00:00:00.000';
    const ms = Math.round(total * 1000);
    const h = Math.floor(ms / 3600000);
    const m = Math.floor((ms % 3600000) / 60000);
    const s = Math.floor((ms % 60000) / 1000);
    const millis = ms % 1000;
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(millis).padStart(3,'0')}`;
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
    loadAppVersion();
    checkLatestVersions();
    if (versionCheckInterval) clearInterval(versionCheckInterval);
    versionCheckInterval = setInterval(checkLatestVersions, VERSION_CHECK_INTERVAL);
    loadTaskHistory();
    loadPools();
    startAutoRefresh();
    switchMobileTab('pools');
}

// Compare two dotted version strings ("x.y.z"). Returns -1 / 0 / 1.
function compareVersions(a, b) {
    const pa = String(a).split('.').map(n => parseInt(n, 10) || 0);
    const pb = String(b).split('.').map(n => parseInt(n, 10) || 0);
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i++) {
        const x = pa[i] || 0, y = pb[i] || 0;
        if (x < y) return -1;
        if (x > y) return 1;
    }
    return 0;
}

// Fetch v-shipper's own running version once from /api/health.
async function loadAppVersion() {
    try {
        const res = await fetch(`${API_BASE}/health`);
        const d = await res.json();
        appVersion = d.version || null;
    } catch (_) { /* leave appVersion null; the update pill simply won't render */ }
    updateShipperUpdatePill();
}

// Fetch a repo's tag list and return the highest semver tag as a dotted string
// ("x.y.z"), or null on any failure. Best-effort: a network error, GitHub rate
// limit, or empty list just means no update pill is shown.
async function fetchLatestTag(url) {
    try {
        const res = await fetch(url);
        if (!res.ok) return null;
        const tags = await res.json();
        let latest = null;
        for (const t of tags) {
            const name = String((t && t.name) || '').replace(/^v/i, '');
            if (!/^\d+(\.\d+)*$/.test(name)) continue; // skip non-version tags
            if (!latest || compareVersions(name, latest) > 0) latest = name;
        }
        return latest;
    } catch (_) {
        return null;
    }
}

// Check GitHub for the latest v-shipper and v-helper releases, then refresh the
// update indicators. Each component is compared against its own latest release —
// the two version lines are independent.
async function checkLatestVersions() {
    const [shipper, helper] = await Promise.all([
        fetchLatestTag(SHIPPER_TAGS_URL),
        fetchLatestTag(HELPER_TAGS_URL),
    ]);
    if (shipper) latestShipperVersion = shipper;
    if (helper) latestHelperVersion = helper;
    updateShipperUpdatePill();
    if (Object.keys(poolsCache).length) displayPools(Object.values(poolsCache));
}

// Header pill flags v-shipper itself when a newer release exists on GitHub.
function updateShipperUpdatePill() {
    const hdrPill = document.getElementById('shipperUpdatePill');
    if (!hdrPill) return;
    const behind = appVersion && latestShipperVersion
        && compareVersions(appVersion, latestShipperVersion) < 0;
    hdrPill.style.display = behind ? '' : 'none';
    if (behind) {
        hdrPill.title = `v-shipper ${appVersion} is behind the latest release ${latestShipperVersion} — update v-shipper`;
    }
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
        const hasHelper = !!pool.has_helper;
        const remoteHasFullStats = isRemote && hasHelper;

        const statsHtml = (!isRemote || remoteHasFullStats)
            ? `<div class="stat"><div class="stat-label">Used</div><div class="stat-value">${pool.used_gb.toFixed(1)} GB</div></div>
               <div class="stat"><div class="stat-label">Free</div><div class="stat-value">${pool.available_gb.toFixed(1)} GB</div></div>`
            : `<div class="stat"><div class="stat-label">Used</div><div class="stat-value">${pool.total_gb.toFixed(1)} GB</div></div>`;
        const usageBarHtml = (!isRemote || remoteHasFullStats) ? `
            <div class="progress-bar" style="margin-top: 8px;">
                <div class="progress-fill" style="width: ${Math.min(usagePercent, 100)}%"></div>
            </div>` : '';
        const reachableHtml = pool.reachable === false
            ? `<div class="pool-unreachable">⚠ Unreachable</div>` : '';
        const helperBadgeHtml = hasHelper
            ? `<span class="pool-helper-badge">v-helper</span>` : '';

        // A connected v-helper behind the latest v-helper release on GitHub gets
        // an "out of date" pill on its card. A reachable helper that reports no
        // version predates the /version endpoint, so it is treated as outdated.
        let helperOutdated = false;
        if (hasHelper && latestHelperVersion) {
            if (!pool.helper_version) {
                helperOutdated = true;
            } else if (compareVersions(pool.helper_version, latestHelperVersion) < 0) {
                helperOutdated = true;
            }
        }
        const updatePillHtml = helperOutdated
            ? `<span class="pool-update-pill" title="v-helper ${escapeHtml(pool.helper_version || 'unknown')} is behind the latest release ${escapeHtml(latestHelperVersion || '')} — update v-helper">out of date</span>`
            : '';

        poolEl.innerHTML = `
            <div style="cursor: pointer;" data-action="select-pool" data-pool="${escapeHtml(pool.name)}">
                <div class="pool-name"><span class="pool-name-text">${escapeHtml(pool.name)}</span>${helperBadgeHtml}${updatePillHtml}</div>
                <div class="pool-type">${pool.role === 'backup' ? 'Backup' : 'Docker'} · ${escapeHtml(pool.pool_type)}</div>
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
    const isLocalDocker = poolRole !== 'backup' && (poolMeta.pool_type !== 'remote' || poolMeta.has_helper);
    const container = document.getElementById('volumesContainer');

    // Backup pools get a grouped archive view
    if (poolRole === 'backup') {
        displayBackupPool(poolName, volumes, warnings);
        return;
    }

    syncBulkSelection(poolName, volumes.map(v => v.name));

    let html = `<div class="volumes-header">
        <h2>${escapeHtml(poolName)}</h2>
        ${isLocalDocker ? `<button class="btn tonal" data-action="open-create-volume" data-pool="${escapeHtml(poolName)}">+ New Volume</button>` : ''}
    </div>
    <div id="bulkToolbar" class="bulk-toolbar"></div>`;
    if (warnings.length > 0) {
        html += `<div class="warning-banner">${warnings.map(w => `<div>${escapeHtml(w)}</div>`).join('')}</div>`;
    }

    if (volumes.length === 0) {
        html += '<div class="placeholder"><p>No volumes found in this pool</p></div>';
    } else {
        volumes.forEach(volume => {
            const selected = bulkSelection.has(volume.name);
            const sizeText = volume.size_loading
                ? `<span class="loading-spinner" style="width: 14px; height: 14px; border-width: 2px;"></span> Calculating...`
                : formatVolumeSize(volume.size_bytes, volume.size_gb);
            const created = formatDate(volume.created_timestamp);
            const backupCount = volume.backups && volume.backups.length > 0 ? volume.backups.length : 0;
            const backupLabel = backupCount ? ` · ${backupCount} backup${backupCount !== 1 ? 's' : ''}` : '';

            const volAttrs = `data-pool="${escapeHtml(poolName)}" data-vol="${escapeHtml(volume.name)}"`;
            html += `
                <div class="volume-item${selected ? ' bulk-selected' : ''}">
                    <input type="checkbox" class="bulk-check" data-vol="${escapeHtml(volume.name)}" aria-label="Select ${escapeHtml(volume.name)}"${selected ? ' checked' : ''}>
                    <div class="volume-info">
                        <div class="volume-name">${escapeHtml(volume.name)}</div>
                        <div class="volume-details">
                            <span class="volume-size">${sizeText}</span>
                            <span class="volume-meta">Created: ${created}${backupLabel}<span class="volume-containers" data-vol-users="${escapeHtml(volume.name)}"></span></span>
                        </div>
                    </div>
                    <div class="volume-actions">
                        <button class="btn vol-btn" title="Migrate" aria-label="Migrate" data-action="open-migrate" ${volAttrs}>🚚</button>
                        <button class="btn vol-btn" title="Backup" aria-label="Backup" data-action="open-backup" ${volAttrs}>💾</button>
                        ${isLocalDocker ? `<button class="btn tonal vol-btn" title="Rename" aria-label="Rename" data-action="open-rename" ${volAttrs}>✏️</button>` : ''}
                        ${isLocalDocker ? `<button class="btn tonal vol-btn" title="Permissions" aria-label="Permissions" data-action="open-permissions" ${volAttrs}>🔑</button>` : ''}
                        <button class="btn danger vol-btn" title="Delete" aria-label="Delete" data-action="open-delete" ${volAttrs}>🗑️</button>
                    </div>
                </div>
            `;
        });
    }

    container.innerHTML = html;
    updateBulkToolbar();

    if (poolMeta.docker_socket && volumes.length) loadVolumeContainers(poolName);
}

// ── Container usage (Docker socket) ───────────────────────────────────────────

async function loadVolumeContainers(poolName) {
    try {
        const res = await fetch(`${API_BASE}/containers?pool=${encodeURIComponent(poolName)}`);
        if (!res.ok) return;
        const map = await res.json();
        containerUsageCache[poolName] = map;
        if (activePool !== poolName) return; // user navigated away
        Object.entries(map).forEach(([vol, containers]) => {
            const el = document.querySelector(`.volume-containers[data-vol-users="${vol}"]`);
            if (el) el.innerHTML = renderContainerBadge(containers);
        });
    } catch (e) { /* network/docker error — leave badges empty */ }
}

function renderContainerBadge(containers) {
    if (!containers || !containers.length) return '';
    const running = containers.filter(c => c.status === 'running').length;
    const total = containers.length;
    const cls = running === total ? 'running' : (running === 0 ? 'stopped' : 'mixed');
    const rows = containers.map(c => {
        const dot = c.status === 'running' ? 'running' : 'stopped';
        return `<div class="container-row"><span class="status-dot ${dot}"></span>${escapeHtml(c.name)}<span class="container-status">${escapeHtml(c.status)}</span></div>`;
    }).join('');
    return ` · <span class="container-badge" data-action="toggle-container-tooltip" tabindex="0" aria-label="Containers using this volume">
        <span class="status-dot ${cls}"></span>${total}
        <span class="container-tooltip">${rows}</span>
    </span>`;
}

// The tooltip is position:fixed (so it escapes the volume row's overflow:hidden and the
// scrollable volumes pane). Compute its viewport coordinates from the badge each time
// it's shown: above the badge by default, flipped below if there's no room, clamped
// horizontally to the viewport.
function positionContainerTip(badge) {
    const tip = badge.querySelector('.container-tooltip');
    if (!tip) return;
    badge.classList.add('show-tip'); // display it first so it can be measured
    const br = badge.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    let left = Math.min(br.left, window.innerWidth - tr.width - 8);
    left = Math.max(8, left);
    let top = br.top - tr.height - 6;
    if (top < 8) top = br.bottom + 6; // flip below when there's no room above
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
}

function hideContainerTip(badge) {
    badge.classList.remove('show-tip');
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
    syncBulkSelection(poolName, archives.map(a => a.name));
    let html = `<div class="volumes-header"><h2>${escapeHtml(poolName)}</h2></div>
    <div id="bulkToolbar" class="bulk-toolbar"></div>`;
    if (warnings.length > 0) {
        html += `<div class="warning-banner">${warnings.map(w => `<div>${escapeHtml(w)}</div>`).join('')}</div>`;
    }
    if (archives.length === 0) {
        html += '<div class="placeholder"><p>No backups in this pool</p></div>';
        container.innerHTML = html;
        updateBulkToolbar();
        return;
    }

    // Two-level grouping: by app (volume) name, then by source pool within each app.
    const apps = {};
    const unparsed = [];
    archives.forEach(a => {
        const parsed = parseBackupFilename(a.name);
        if (!parsed) { unparsed.push(a); return; }
        const appKey = parsed.volume || parsed.pool; // whole-pool backups fall back to the pool name
        if (!apps[appKey]) apps[appKey] = { label: appKey, pools: {}, count: 0 };
        if (!apps[appKey].pools[parsed.pool]) apps[appKey].pools[parsed.pool] = [];
        apps[appKey].pools[parsed.pool].push({ archive: a, parsed });
        apps[appKey].count++;
    });

    // Sort apps alphabetically by label
    const sortedApps = Object.entries(apps).sort(([, a], [, b]) => a.label.localeCompare(b.label));

    sortedApps.forEach(([, app]) => {
        html += `<div class="backup-group">
            <div class="backup-group-header">${escapeHtml(app.label)} <span>${app.count} backup${app.count !== 1 ? 's' : ''}</span></div>`;
        // Sub-group by source pool, sorted alphabetically
        Object.keys(app.pools).sort((a, b) => a.localeCompare(b)).forEach(poolKey => {
            const items = app.pools[poolKey].sort((a, b) => b.parsed.timestamp - a.parsed.timestamp);
            html += `<div class="backup-subgroup">
                <div class="backup-subgroup-header">from ${escapeHtml(poolKey)} <span>· ${items.length} backup${items.length !== 1 ? 's' : ''}</span></div>
                <div class="backup-group-items">`;
            items.forEach(({ archive: a, parsed }) => {
                const dt = `${String(parsed.timestamp.getDate()).padStart(2,'0')}/${String(parsed.timestamp.getMonth()+1).padStart(2,'0')}/${parsed.timestamp.getFullYear()} ${String(parsed.timestamp.getHours()).padStart(2,'0')}:${String(parsed.timestamp.getMinutes()).padStart(2,'0')}`;
                html += renderBackupItem(poolName, a, dt);
            });
            html += `</div></div>`;
        });
        html += `</div>`;
    });

    if (unparsed.length > 0) {
        html += `<div class="backup-group">
            <div class="backup-group-header">Other <span>${unparsed.length} file${unparsed.length !== 1 ? 's' : ''}</span></div>
            <div class="backup-group-items">`;
        unparsed.forEach(a => {
            html += renderBackupItem(poolName, a, '');
        });
        html += `</div></div>`;
    }

    container.innerHTML = html;
    updateBulkToolbar();
}

function renderBackupItem(poolName, archive, dateLabel) {
    const size = formatVolumeSize(archive.size_bytes, archive.size_gb);
    const meta = dateLabel ? `${dateLabel} · ${size}` : size;
    const selected = bulkSelection.has(archive.name);
    return `<div class="backup-item${selected ? ' bulk-selected' : ''}">
        <input type="checkbox" class="bulk-check" data-file="${escapeHtml(archive.name)}" aria-label="Select ${escapeHtml(archive.name)}"${selected ? ' checked' : ''}>
        <div class="backup-item-info">
            <div class="backup-item-name">${escapeHtml(archive.name)}</div>
            <div class="backup-item-meta">${meta}</div>
        </div>
        <div class="backup-item-actions">
            <button class="btn vol-btn" title="Restore" aria-label="Restore" data-action="open-restore" data-pool="${escapeHtml(poolName)}" data-file="${escapeHtml(archive.name)}">📥</button>
            <button class="btn danger vol-btn" title="Delete" aria-label="Delete" data-action="open-delete" data-pool="${escapeHtml(poolName)}" data-vol="${escapeHtml(archive.name)}">🗑️</button>
        </div>
    </div>`;
}

// ── Bulk actions ───────────────────────────────────────────────────────────────
// Checkboxes on each volume/backup row feed a selection set; when non-empty a
// toolbar of bulk actions appears above the list. Each bulk action reuses the
// matching single-item modal (with the item field shown as a list) and posts to
// /api/bulk/*, which runs the items sequentially under one summary task.

// Keep the selection tied to the pool being viewed: reset it when the pool
// changes, and drop any names that no longer exist after a refresh.
function syncBulkSelection(poolName, availableNames) {
    if (bulkSelectionPool !== poolName) {
        bulkSelection.clear();
        bulkSelectionPool = poolName;
    }
    const avail = new Set(availableNames);
    [...bulkSelection].forEach(n => { if (!avail.has(n)) bulkSelection.delete(n); });
}

function clearBulkSelection() {
    bulkSelection.clear();
    document.querySelectorAll('.bulk-check').forEach(cb => { cb.checked = false; });
    document.querySelectorAll('.bulk-selected').forEach(r => r.classList.remove('bulk-selected'));
    updateBulkToolbar();
}

// Render the action toolbar based on the current selection and pool type.
function updateBulkToolbar() {
    const bar = document.getElementById('bulkToolbar');
    if (!bar) return;
    const count = bulkSelection.size;
    if (count === 0) { bar.style.display = 'none'; bar.innerHTML = ''; return; }

    const poolMeta = poolsCache[activePool] || {};
    const isBackupView = poolMeta.role === 'backup';
    const isLocalDocker = poolMeta.role !== 'backup' && (poolMeta.pool_type !== 'remote' || poolMeta.has_helper);

    const btns = isBackupView
        ? `<button class="btn vol-btn" data-action="bulk-open-restore">📥 Restore</button>
           <button class="btn danger vol-btn" data-action="bulk-open-delete">🗑️ Delete</button>`
        : `<button class="btn vol-btn" data-action="bulk-open-backup">💾 Backup</button>
           <button class="btn vol-btn" data-action="bulk-open-migrate">🚚 Migrate</button>
           ${isLocalDocker ? `<button class="btn tonal vol-btn" data-action="bulk-open-permissions">🔑 Permissions</button>` : ''}
           <button class="btn danger vol-btn" data-action="bulk-open-delete">🗑️ Delete</button>`;

    bar.style.display = '';
    bar.innerHTML = `
        <span class="bulk-toolbar-count">${count} selected</span>
        <div class="bulk-toolbar-actions">${btns}</div>
        <div class="bulk-toolbar-right">
            <button class="btn tonal" data-action="bulk-select-all">Select all</button>
            <button class="btn tonal" data-action="bulk-clear">Clear</button>
        </div>`;
}

// Select every item currently rendered in the list (volumes or backups).
function selectAllBulk() {
    document.querySelectorAll('.bulk-check').forEach(cb => {
        const name = cb.dataset.vol || cb.dataset.file;
        if (name) bulkSelection.add(name);
        cb.checked = true;
        const row = cb.closest('.volume-item, .backup-item');
        if (row) row.classList.add('bulk-selected');
    });
    updateBulkToolbar();
}

// Toggle selection when a row checkbox changes (delegated so it survives re-renders).
document.addEventListener('change', (e) => {
    const cb = e.target.closest('.bulk-check');
    if (!cb) return;
    const name = cb.dataset.vol || cb.dataset.file;
    if (!name) return;
    if (cb.checked) bulkSelection.add(name); else bulkSelection.delete(name);
    const row = cb.closest('.volume-item, .backup-item');
    if (row) row.classList.toggle('bulk-selected', cb.checked);
    updateBulkToolbar();
});

// Scrollable read-only list of the selected items, shown in place of the single
// "Source Volume" / "Volume" / "Backup File" field.
function bulkItemsListHtml(label) {
    const items = [...bulkSelection].sort();
    return `<div class="form-group">
        <label>${label} (${items.length})</label>
        <div class="bulk-items-list">${items.map(n => `<div class="bulk-items-row">${escapeHtml(n)}</div>`).join('')}</div>
    </div>`;
}

// Generic stop/start-container checkboxes for bulk modals. Uses the same ids the
// single modals do so readContainerControl() picks them up.
function bulkContainerControlHtml({ start } = {}) {
    const startRow = start ? `
        <div class="form-group">
            <label class="checkbox-label"><input type="checkbox" id="ccStartAfter"><span>Start container(s) after</span></label>
        </div>` : '';
    return `
        <div class="form-group">
            <label class="checkbox-label"><input type="checkbox" id="ccStopBefore"><span>Stop container(s) before each operation</span></label>
        </div>${startRow}`;
}

async function _submitBulk(endpoint, body, modalId) {
    try {
        const res = await fetch(`${API_BASE}/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (res.ok) {
            closeModal(modalId);
            clearBulkSelection();
            if (activePool) loadVolumesForPool(activePool);
            showTaskProgress(data.task_id);
        } else {
            showError(typeof data.detail === 'string' ? data.detail : 'Bulk action failed');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

function openBulkBackupModal() {
    if (!bulkSelection.size) return;
    const pool = activePool;
    document.getElementById('backupModal').querySelector('.modal-content').innerHTML = `
        <div class="modal-header"><h3>Bulk Backup</h3><button class="close-btn" onclick="closeModal('backupModal')">×</button></div>
        <div class="form-group"><label>Source Pool</label><input type="text" value="${escapeHtml(pool)}" disabled></div>
        ${bulkItemsListHtml('Source Volumes')}
        <div class="form-group"><label>Backup Pool</label><select id="bulkBackupPool"><option value="">-- Select backup pool --</option></select></div>
        <div class="form-group"><label class="checkbox-label"><input type="checkbox" id="bulkBackupVerify" checked><span>Verify backup</span></label></div>
        ${bulkContainerControlHtml({ start: true })}
        <button class="btn success" style="width:100%;" data-action="start-bulk-backup">Start Bulk Backup</button>`;
    loadBackupPoolsForSelect('bulkBackupPool');
    openModal('backupModal');
}

function startBulkBackup() {
    const backupPool = document.getElementById('bulkBackupPool').value;
    if (!backupPool) { showError('Please select a backup pool'); return; }
    _submitBulk('bulk/backup', {
        source_pool: activePool,
        source_volumes: [...bulkSelection],
        backup_pool: backupPool,
        verify: document.getElementById('bulkBackupVerify').checked,
        ...readContainerControl(),
    }, 'backupModal');
}

function openBulkMigrateModal() {
    if (!bulkSelection.size) return;
    const pool = activePool;
    document.getElementById('migrateModal').querySelector('.modal-content').innerHTML = `
        <div class="modal-header"><h3>Bulk Migrate</h3><button class="close-btn" onclick="closeModal('migrateModal')">×</button></div>
        <div class="form-group"><label>Source Pool</label><input type="text" value="${escapeHtml(pool)}" disabled></div>
        ${bulkItemsListHtml('Source Volumes')}
        <div class="form-group"><label>Destination Pool</label><select id="bulkDestPool"><option value="">-- Select pool --</option></select></div>
        <div class="form-group"><label>If destination exists</label><select id="bulkMigrateConflict">
            <option value="skip">Skip that volume</option>
            <option value="overwrite">Overwrite</option>
        </select></div>
        <div class="form-group"><label class="checkbox-label"><input type="checkbox" id="bulkMigrateVerify" checked><span>Verify migration</span></label></div>
        <div class="form-group"><label class="checkbox-label"><input type="checkbox" id="bulkMigrateDelete"><span>Delete source after verification</span></label></div>
        ${bulkContainerControlHtml({ start: true })}
        <button class="btn success" style="width:100%;" data-action="start-bulk-migrate">Start Bulk Migration</button>`;
    loadPoolsForSelect('bulkDestPool');
    openModal('migrateModal');
}

function startBulkMigrate() {
    const destPool = document.getElementById('bulkDestPool').value;
    if (!destPool) { showError('Please select a destination pool'); return; }
    _submitBulk('bulk/migrate', {
        source_pool: activePool,
        source_volumes: [...bulkSelection],
        dest_pool: destPool,
        verify: document.getElementById('bulkMigrateVerify').checked,
        delete_source: document.getElementById('bulkMigrateDelete').checked,
        conflict_resolution: document.getElementById('bulkMigrateConflict').value,
        ...readContainerControl(),
    }, 'migrateModal');
}

function openBulkPermissionsModal() {
    if (!bulkSelection.size) return;
    const pool = activePool;
    document.getElementById('migrateModal').querySelector('.modal-content').innerHTML = `
        <div class="modal-header"><h3>Bulk Permissions</h3><button class="close-btn" onclick="closeModal('migrateModal')">×</button></div>
        <div class="form-group"><label>Pool</label><input type="text" value="${escapeHtml(pool)}" disabled></div>
        ${bulkItemsListHtml('Volumes')}
        <p class="settings-section-desc">The same chmod/chown is applied recursively to every selected volume. Leave a field blank to skip it.</p>
        <div class="form-group"><label>User / UID</label><input type="text" id="bulkPermUser" maxlength="32" placeholder="(unchanged)"></div>
        <div class="form-group"><label>Group / GID</label><input type="text" id="bulkPermGroup" maxlength="32" placeholder="(unchanged)"></div>
        <div class="form-group"><label>Permission (octal)</label><input type="text" id="bulkPermMode" maxlength="4" pattern="[0-7]{3,4}" title="Octal mode, e.g. 755" placeholder="(unchanged)"></div>
        ${bulkContainerControlHtml({ start: true })}
        <button class="btn success" style="width:100%;" data-action="start-bulk-permissions">Apply to ${bulkSelection.size} volume(s)</button>`;
    openModal('migrateModal');
}

function startBulkPermissions() {
    const user = document.getElementById('bulkPermUser').value.trim();
    const group = document.getElementById('bulkPermGroup').value.trim();
    const mode = document.getElementById('bulkPermMode').value.trim();
    if ((user || group) && (!user || !group)) {
        showError('User and Group are both required for an ownership change');
        return;
    }
    if (!mode && !user) { showError('Enter a mode and/or an owner/group to apply'); return; }
    const body = {
        pool: activePool,
        volumes: [...bulkSelection],
        ...readContainerControl(),
    };
    if (mode) body.mode = mode;
    if (user) { body.owner = user; body.group = group; }
    _submitBulk('bulk/permissions', body, 'migrateModal');
}

function openBulkDeleteModal() {
    if (!bulkSelection.size) return;
    const pool = activePool;
    document.getElementById('deleteModal').querySelector('.modal-content').innerHTML = `
        <div class="modal-header"><h3>Bulk Delete</h3><button class="close-btn" onclick="closeModal('deleteModal')">×</button></div>
        <p><strong>Warning:</strong> This permanently deletes every selected item. This cannot be undone.</p>
        <div class="form-group"><label>Pool</label><input type="text" value="${escapeHtml(pool)}" disabled></div>
        ${bulkItemsListHtml('Items')}
        <label class="checkbox-label"><input type="checkbox" id="bulkDeleteConfirm"><span>Yes, delete the ${bulkSelection.size} selected item(s)</span></label>
        ${bulkContainerControlHtml({ start: false })}
        <button class="btn danger" style="width:100%; margin-top:15px;" data-action="confirm-bulk-delete">Delete</button>`;
    openModal('deleteModal');
}

function confirmBulkDelete() {
    if (!document.getElementById('bulkDeleteConfirm').checked) {
        showError('Please confirm deletion');
        return;
    }
    _submitBulk('bulk/delete', {
        pool: activePool,
        volumes: [...bulkSelection],
        confirm: true,
        stop_containers_before: !!document.getElementById('ccStopBefore')?.checked,
    }, 'deleteModal');
}

function openBulkRestoreModal() {
    if (!bulkSelection.size) return;
    const pool = activePool;
    document.getElementById('backupModal').querySelector('.modal-content').innerHTML = `
        <div class="modal-header"><h3>Bulk Restore</h3><button class="close-btn" onclick="closeModal('backupModal')">×</button></div>
        <div class="form-group"><label>Backup Pool</label><input type="text" value="${escapeHtml(pool)}" disabled></div>
        ${bulkItemsListHtml('Backup Files')}
        <p class="settings-section-desc">Each backup restores to a volume named after its file (pool prefix and timestamp stripped).</p>
        <div class="form-group"><label>Destination Pool</label><select id="bulkRestorePool"><option value="">-- Select destination pool --</option></select></div>
        <div class="form-group"><label>If destination exists</label><select id="bulkRestoreConflict">
            <option value="skip">Skip that backup</option>
            <option value="overwrite">Overwrite</option>
            <option value="merge">Merge into existing</option>
        </select></div>
        <button class="btn success" style="width:100%;" data-action="start-bulk-restore">Start Bulk Restore</button>`;
    loadPoolsForSelect('bulkRestorePool');
    openModal('backupModal');
}

function startBulkRestore() {
    const destPool = document.getElementById('bulkRestorePool').value;
    if (!destPool) { showError('Please select a destination pool'); return; }
    _submitBulk('bulk/restore', {
        backup_pool: activePool,
        backup_files: [...bulkSelection],
        dest_pool: destPool,
        conflict_resolution: document.getElementById('bulkRestoreConflict').value,
    }, 'backupModal');
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
                <input type="text" id="newVolumeName" placeholder="my-volume" maxlength="255" pattern="${VOLUME_NAME_PATTERN}" title="${VOLUME_NAME_TITLE}" autofocus>
            </div>
            <button class="btn success" style="width:100%;" data-action="create-volume" data-pool="${escapeHtml(poolName)}">Create</button>
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
            loadVolumesForPool(poolName);
            showTaskProgress(data.task_id);
        } else {
            showError(data.detail || 'Failed to create volume');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

// Warning banner shown in migrate/rename/delete modals when the volume has running
// containers. Reads the cached container-usage map; best-effort (empty → no banner).
function runningContainerWarningHtml(poolName, volumeName, verb) {
    const list = (containerUsageCache[poolName] || {})[volumeName] || [];
    const running = list.filter(c => c.status === 'running');
    if (!running.length) return '';
    const names = running.map(c => escapeHtml(c.name)).join(', ');
    return `<div class="warning-banner">⚠ In use by running container${running.length !== 1 ? 's' : ''}: ${names}. ${verb} may cause corruption or downtime.</div>`;
}

// Stop/start-container checkboxes for an operation modal. Rendered only when the
// volume is used by a running container (read from the cached usage map), so the
// option appears only when it's actionable. `start` controls whether the
// start-after checkbox is included (rename/delete pass false). The submit handlers
// read the resulting checkboxes via readContainerControl().
function containerControlCheckboxesHtml(poolName, volumeName, { start } = {}) {
    const list = (containerUsageCache[poolName] || {})[volumeName] || [];
    if (!list.some(c => c.status === 'running')) return '';
    const startRow = start ? `
        <div class="form-group">
            <label class="checkbox-label">
                <input type="checkbox" id="ccStartAfter">
                <span>Start container(s) after</span>
            </label>
        </div>` : '';
    return `
        <div class="form-group">
            <label class="checkbox-label">
                <input type="checkbox" id="ccStopBefore">
                <span>Stop container(s) before</span>
            </label>
        </div>${startRow}`;
}

// Read the container-control checkboxes (absent → false). Handlers spread or pick
// from this when building the request body.
function readContainerControl() {
    return {
        stop_containers_before: !!document.getElementById('ccStopBefore')?.checked,
        start_containers_after: !!document.getElementById('ccStartAfter')?.checked,
    };
}

function openRenameVolumeModal(poolName, volumeName) {
    const modal = document.getElementById('migrateModal');
    modal.querySelector('.modal-content').innerHTML = `
        <div class="modal-header">
            <h3>Rename Volume</h3>
            <button class="close-btn" onclick="closeModal('migrateModal')">×</button>
        </div>
        <div class="modal-body">
            ${runningContainerWarningHtml(poolName, volumeName, 'Renaming')}
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
                <input type="text" id="renameVolumeName" value="${escapeHtml(volumeName)}" maxlength="255" pattern="${VOLUME_NAME_PATTERN}" title="${VOLUME_NAME_TITLE}" autofocus>
            </div>
            ${containerControlCheckboxesHtml(poolName, volumeName, { start: false })}
            <button class="btn success" style="width:100%;" data-action="rename-volume" data-pool="${escapeHtml(poolName)}" data-vol="${escapeHtml(volumeName)}">Rename</button>
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
            body: JSON.stringify({ pool: poolName, old_name: oldName, new_name: newName, stop_containers_before: readContainerControl().stop_containers_before }),
        });
        const data = await res.json();
        if (res.ok) {
            closeModal('migrateModal');
            loadVolumesForPool(poolName);
            showTaskProgress(data.task_id);
        } else {
            showError(data.detail || 'Failed to rename volume');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

let _permissionDefaults = null;

async function openPermissionsModal(poolName, volumeName) {
    let perms;
    try {
        const res = await fetch(`${API_BASE}/permissions?pool=${encodeURIComponent(poolName)}&volume=${encodeURIComponent(volumeName)}`);
        const data = await res.json();
        if (!res.ok) { showError(data.detail || 'Failed to read current permissions'); return; }
        perms = data;
    } catch (e) {
        showError(`Error: ${e.message}`);
        return;
    }
    _permissionDefaults = { user: String(perms.user), group: String(perms.group), mode: String(perms.mode) };

    const modal = document.getElementById('migrateModal');
    modal.querySelector('.modal-content').innerHTML = `
        <div class="modal-header">
            <h3>Edit Permissions</h3>
            <button class="close-btn" onclick="closeModal('migrateModal')">×</button>
        </div>
        <div class="modal-body">
            <div class="form-group">
                <label>Pool</label>
                <input type="text" value="${escapeHtml(poolName)}" disabled>
            </div>
            <div class="form-group">
                <label>Volume</label>
                <input type="text" value="${escapeHtml(volumeName)}" disabled>
            </div>
            <div class="form-group">
                <label>User / UID</label>
                <input type="text" id="permUser" value="${escapeHtml(_permissionDefaults.user)}" maxlength="32">
            </div>
            <div class="form-group">
                <label>Group / GID</label>
                <input type="text" id="permGroup" value="${escapeHtml(_permissionDefaults.group)}" maxlength="32">
            </div>
            <div class="form-group">
                <label>Permission (octal)</label>
                <input type="text" id="permMode" value="${escapeHtml(_permissionDefaults.mode)}" maxlength="4" pattern="[0-7]{3,4}" title="Octal mode, e.g. 755">
            </div>
            <p class="hint" style="opacity:.7;font-size:12px;">Applied recursively (-R). By default only changed fields are applied.</p>
            <div class="form-group">
                <label class="checkbox-label">
                    <input type="checkbox" id="permForceMode">
                    <span>Apply chmod even if unchanged (re-apply recursively)</span>
                </label>
            </div>
            <div class="form-group">
                <label class="checkbox-label">
                    <input type="checkbox" id="permForceOwner">
                    <span>Apply chown even if unchanged (re-apply recursively)</span>
                </label>
            </div>
            ${containerControlCheckboxesHtml(poolName, volumeName, { start: true })}
            <button class="btn success" style="width:100%;" data-action="save-permissions" data-pool="${escapeHtml(poolName)}" data-vol="${escapeHtml(volumeName)}">Apply</button>
        </div>`;
    openModal('migrateModal');
}

async function savePermissions(poolName, volumeName) {
    if (!_permissionDefaults) { closeModal('migrateModal'); return; }
    const user = document.getElementById('permUser')?.value.trim();
    const group = document.getElementById('permGroup')?.value.trim();
    const mode = document.getElementById('permMode')?.value.trim();

    const forceMode = !!document.getElementById('permForceMode')?.checked;
    const forceOwner = !!document.getElementById('permForceOwner')?.checked;
    const modeChanged = mode && mode !== _permissionDefaults.mode;
    const ownerChanged = (user && user !== _permissionDefaults.user) || (group && group !== _permissionDefaults.group);

    const body = { pool: poolName, volume_name: volumeName };
    if (mode && (forceMode || modeChanged)) body.mode = mode;
    if ((user || group) && (forceOwner || ownerChanged)) {
        if (!user || !group) { showError('User and Group are both required for an ownership change'); return; }
        body.owner = user;
        body.group = group;
    }

    if (!body.mode && !body.owner) { closeModal('migrateModal'); return; }
    Object.assign(body, readContainerControl());

    try {
        const res = await fetch(`${API_BASE}/permissions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (res.ok) {
            closeModal('migrateModal');
            loadVolumesForPool(poolName);
            showTaskProgress(data.task_id);
        } else {
            showError(data.detail || 'Failed to change permissions');
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
        ${runningContainerWarningHtml(sourcePool, sourceVolume, 'Migrating')}
        <div class="form-group">
            <label>Source Pool</label>
            <input type="text" value="${escapeHtml(sourcePool)}" disabled>
        </div>
        <div class="form-group">
            <label>Source Volume</label>
            <input type="text" value="${escapeHtml(sourceVolume)}" disabled>
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
        ${containerControlCheckboxesHtml(sourcePool, sourceVolume, { start: true })}
        <button class="btn success" style="width: 100%;" data-action="start-migration" data-pool="${escapeHtml(sourcePool)}" data-vol="${escapeHtml(sourceVolume)}">Start Migration</button>
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
        ...readContainerControl(),
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
            <input type="text" value="${escapeHtml(sourcePool)}" disabled>
        </div>
        <div class="form-group">
            <label>Source Volume</label>
            <input type="text" value="${escapeHtml(sourceVolume)}" disabled>
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
        ${containerControlCheckboxesHtml(sourcePool, sourceVolume, { start: true })}
        <button class="btn success" style="width: 100%;" data-action="start-backup" data-pool="${escapeHtml(sourcePool)}" data-vol="${escapeHtml(sourceVolume)}">Start Backup</button>
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
                verify: verify,
                ...readContainerControl(),
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
            <input type="text" value="${escapeHtml(backupPool)}" disabled>
        </div>
        <div class="form-group">
            <label>Backup File</label>
            <input type="text" value="${escapeHtml(backupFile)}" disabled>
        </div>
        <div class="form-group">
            <label>Destination Pool</label>
            <select id="restorePool">
                <option value="">-- Select destination pool --</option>
            </select>
        </div>
        <div class="form-group">
            <label>Volume Name</label>
            <input type="text" id="restoreVolumeName" value="${escapeHtml(defaultVolume)}" maxlength="255" pattern="${VOLUME_NAME_PATTERN}" title="${VOLUME_NAME_TITLE}" required>
        </div>
        <button class="btn success" style="width: 100%;" data-action="start-restore" data-pool="${escapeHtml(backupPool)}" data-file="${escapeHtml(backupFile)}">Start Restore</button>
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
        ${runningContainerWarningHtml(pool, volume, 'Deleting')}
        <p><strong>Warning:</strong> This action cannot be undone. Make sure you have a backup if needed.</p>
        <div class="form-group">
            <label>Pool</label>
            <input type="text" value="${escapeHtml(pool)}" disabled>
        </div>
        <div class="form-group">
            <label>Volume</label>
            <input type="text" value="${escapeHtml(volume)}" disabled>
        </div>
        <label class="checkbox-label">
            <input type="checkbox" id="deleteConfirm">
            <span>Yes, I want to delete this volume</span>
        </label>
        ${containerControlCheckboxesHtml(pool, volume, { start: false })}
        <button class="btn danger" id="confirmDeleteButton" style="width: 100%; margin-top: 15px;" data-action="confirm-delete" data-pool="${escapeHtml(pool)}" data-vol="${escapeHtml(volume)}">Delete</button>
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
                confirm: confirmed,
                stop_containers_before: readContainerControl().stop_containers_before,
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
            
            if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled') {
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
    if (task.task_type && task.task_type.startsWith('bulk_')) {
        return params.label || `Bulk ${task.task_type.slice(5)} — ${params.total_items || 0} item(s)`;
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
    if (task.task_type === 'rename') {
        const pool = params.pool || '';
        const vol = params.volume_name || '';
        const newName = params.new_name || '';
        return pool ? `${pool}/${vol} → ${newName}` : `${vol} → ${newName}`;
    }
    if (task.task_type === 'create') {
        const pool = params.pool || '';
        const vol = params.volume_name || '';
        return pool && vol ? `${pool}/${vol}` : (vol || 'Create volume');
    }
    return params.source_volume || params.volume_name || params.backup_file || task.current_operation || 'Task';
}

function renderTaskHistory() {
    const progressPanel = document.getElementById('progressPanel');

    // Sub-tasks spawned by a scheduled run or a bulk action are hidden here — they
    // live inside their parent task's detail view instead.
    const visibleTasks = taskHistory.filter(task => !(task.params && (task.params.scheduled || task.params.parent_task_id)));

    if (visibleTasks.length === 0) {
        progressPanel.innerHTML = '<div class="placeholder-text">No active tasks</div>';
        return;
    }

    const totalPages = Math.ceil(visibleTasks.length / TASKS_PER_PAGE);
    taskPage = Math.max(0, Math.min(taskPage, totalPages - 1));
    const pageTasks = visibleTasks.slice(taskPage * TASKS_PER_PAGE, (taskPage + 1) * TASKS_PER_PAGE);

    const items = pageTasks.map(task => {
        const targetLabel = getTaskTargetLabel(task);
        const typeClass = (task.task_type || 'operation').replace(/[^a-z0-9_]/gi, '_');
        return `
            <div class="task-history-item" data-action="open-task-detail" data-id="${escapeHtml(task.task_id)}" title="Click for details">
                <div class="task-card-pills">
                    <div class="task-type-pill type-${typeClass}">${getTaskTypeDisplay(task.task_type)}</div>
                    <div class="task-status ${task.status}">${task.status.toUpperCase()}</div>
                </div>
                <div class="progress-text task-target">${targetLabel}</div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${task.progress_percent}%"></div>
                </div>
                <div class="task-meta">${task.progress_percent}% · ${formatDuration(task.elapsed_seconds)}${task.estimated_remaining_seconds ? ` · ${formatDuration(task.estimated_remaining_seconds)} left` : ''}</div>
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
        bulk_backup: 'Bulk Backup',
        bulk_migrate: 'Bulk Migrate',
        bulk_delete: 'Bulk Delete',
        bulk_permissions: 'Bulk Permissions',
        bulk_restore: 'Bulk Restore',
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
    } else if (name === 'notifications') {
        content.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                <h2 class="settings-section-title" style="margin:0;">Notifications</h2>
                <button class="btn" onclick="openNotificationForm()">+ New Notification</button>
            </div>
            <p class="settings-section-desc">Send Telegram alerts when tasks complete or fail.</p>
            <div id="notificationList" class="notification-list"><div class="placeholder-text">Loading…</div></div>`;
        loadNotifications();
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
                <span class="schedule-enabled-chip ${enabledClass}" data-action="toggle-schedule" data-id="${escapeHtml(job.id)}">${enabledLabel}</span>
                <button class="btn tonal" data-action="run-schedule" data-id="${escapeHtml(job.id)}">▶ Run</button>
                <button class="btn tonal" data-action="edit-schedule" data-id="${escapeHtml(job.id)}">Edit</button>
                <button class="btn danger" data-action="delete-schedule" data-id="${escapeHtml(job.id)}">Delete</button>
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

    // Detect volumes in this schedule that no longer exist. A volume is missing
    // either because it was removed from an existing pool, OR because its whole
    // pool is gone from the config (an "orphan" pool no longer in dockerPools).
    // Both cases must be surfaced as removable checkboxes, otherwise a volume
    // from a deleted pool would vanish from the UI yet linger in the schedule.
    const jobVols = job?.volumes || [];
    const knownPoolNames = new Set(dockerPools.map(p => p.name));
    const orphanPoolNames = [...new Set(
        jobVols.map(v => v.pool).filter(name => !knownPoolNames.has(name))
    )];

    const allMissingVols = jobVols
        .filter(v => !(volumesByPool[v.pool] || []).includes(v.volume))
        .map(v => `${v.pool}/${v.volume}`);

    // Render one group per pool. `missingInPool` are volumes checked in the
    // schedule but absent from the live pool listing (or in an orphan pool,
    // where the pool itself no longer exists so every scheduled volume is missing).
    const renderPoolGroup = (poolName, currentVols, missingInPool) => {
        const allVols = [...currentVols, ...missingInPool];
        if (!allVols.length) return '';
        const poolGone = !knownPoolNames.has(poolName);
        return `<div>
            <div class="schedule-pool-group-title">${escapeHtml(poolName)}${poolGone ? ' <span class="schedule-vol-missing-badge">⚠ pool not found</span>' : ''}</div>
            ${allVols.map(v => {
                const key = `${poolName}::${v}`;
                const checked = selectedVols.has(key) ? 'checked' : '';
                const missing = missingInPool.includes(v);
                return `<label class="schedule-vol-item${missing ? ' schedule-vol-missing' : ''}">
                    <input type="checkbox" data-pool="${escapeHtml(poolName)}" data-vol="${escapeHtml(v)}" ${checked}>
                    ${escapeHtml(v)}${missing ? ' <span class="schedule-vol-missing-badge">⚠ not found</span>' : ''}
                </label>`;
            }).join('')}
        </div>`;
    };

    const volumeGroupsHtml = [
        ...dockerPools.map(p => {
            const currentVols = volumesByPool[p.name] || [];
            const missingInPool = jobVols
                .filter(v => v.pool === p.name && !currentVols.includes(v.volume))
                .map(v => v.volume);
            return renderPoolGroup(p.name, currentVols, missingInPool);
        }),
        ...orphanPoolNames.map(poolName => {
            const missingVols = jobVols
                .filter(v => v.pool === poolName)
                .map(v => v.volume);
            return renderPoolGroup(poolName, [], missingVols);
        }),
    ].join('');

    const missingWarning = allMissingVols.length
        ? `<div class="warning-banner" style="margin-bottom:12px;">⚠ ${allMissingVols.length} volume${allMissingVols.length !== 1 ? 's' : ''} in this schedule no longer exist. Uncheck them and save to remove from the schedule.</div>`
        : '';

    content.innerHTML = `
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px;">
            <button class="btn tonal" onclick="showSettingsSection('schedules')">← Back</button>
            <h2 class="settings-section-title" style="margin:0;">${jobId ? 'Edit Schedule' : 'New Schedule'}</h2>
        </div>
        ${missingWarning}
        <div class="schedule-form">
            <label>Name
                <input type="text" id="sfName" value="${escapeHtml(job?.name || '')}" maxlength="255" pattern="${VOLUME_NAME_PATTERN}" title="${VOLUME_NAME_TITLE}" placeholder="e.g. Nightly_Production_Backup">
            </label>
            <label>Cron Expression
                <input type="text" id="sfCron" value="${escapeHtml(job?.cron || '0 2 * * *')}" maxlength="120" placeholder="0 2 * * *">
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
            <label class="checkbox-label">
                <input type="checkbox" id="sfStopBefore" ${job?.stop_containers_before ? 'checked' : ''}>
                <span>Stop container(s) before each volume's backup</span>
            </label>
            <label class="checkbox-label">
                <input type="checkbox" id="sfStartAfter" ${job?.start_containers_after ? 'checked' : ''}>
                <span>Start container(s) after each volume's backup</span>
            </label>
            <label>Volumes to Back Up
                <div class="schedule-volume-groups" id="sfVolumeGroups">
                    ${volumeGroupsHtml || '<div class="placeholder-text">No volumes available</div>'}
                </div>
            </label>
            <div class="schedule-form-actions">
                <button class="btn success" data-action="save-schedule"${jobId ? ` data-id="${escapeHtml(jobId)}"` : ''}>Save</button>
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

    const body = {
        name, cron, backup_pool: backupPool, volumes, retention,
        stop_containers_before: !!document.getElementById('sfStopBefore')?.checked,
        start_containers_after: !!document.getElementById('sfStartAfter')?.checked,
    };
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

// ── Notifications ─────────────────────────────────────────────────────────────

const NOTIFICATION_TOPICS = [
    { id: 'schedule', label: 'Scheduled Backup', desc: 'When a scheduled backup job completes' },
    { id: 'backup',   label: 'Backup',            desc: 'When a manual backup completes' },
    { id: 'migrate',  label: 'Migrate',           desc: 'When a volume migration completes' },
    { id: 'restore',  label: 'Restore',           desc: 'When a backup restore completes' },
    { id: 'delete',   label: 'Delete',            desc: 'When a volume is deleted' },
    { id: 'rename',   label: 'Rename',            desc: 'When a volume is renamed' },
    { id: 'create',   label: 'Create',            desc: 'When a new volume is created' },
    { id: 'permissions', label: 'Permissions',    desc: 'When a volume\'s permissions/ownership change completes' },
];

const NOTIFICATION_DEFAULT_TEMPLATE = [
    '\u{1F514} *{task_type_label}* {status_emoji}',
    '`{target}`',
    '',
    'Status: *{status}*',
    '⏱ {elapsed}',
    '⏱ Started: {started_at}',
    '\u{1F3C1} Finished: {timestamp}',
    '\u{1F5A5} Host: {hostname}',
    '{params_block}',
    '{error_block}',
].join('\n');

async function loadNotifications() {
    const list = document.getElementById('notificationList');
    if (!list) return;
    try {
        const res = await fetch(`${API_BASE}/notifications`, { headers: { 'Authorization': `Bearer ${sessionId}` } });
        const data = await res.json();
        if (res.ok) {
            renderNotificationList(data.notifications || []);
        } else {
            list.innerHTML = `<div class="placeholder-text">Failed to load notifications</div>`;
        }
    } catch (e) {
        if (list) list.innerHTML = `<div class="placeholder-text">Error: ${escapeHtml(e.message)}</div>`;
    }
}

function renderNotificationList(configs) {
    const list = document.getElementById('notificationList');
    if (!list) return;
    if (!configs.length) {
        list.innerHTML = `<div class="placeholder-text">No notifications yet. Click "+ New Notification" to create one.</div>`;
        return;
    }
    list.innerHTML = configs.map(cfg => {
        const topicChips = (cfg.topics || [])
            .map(t => {
                const topic = NOTIFICATION_TOPICS.find(x => x.id === t);
                return `<span class="topic-chip">${escapeHtml(topic ? topic.label : t)}</span>`;
            }).join('');
        const failureLabel = cfg.on_failure_only ? ' · Failures only' : '';
        const enabledClass = cfg.enabled ? 'on' : 'off';
        const enabledLabel = cfg.enabled ? 'Enabled' : 'Disabled';
        return `
        <div class="notification-row">
            <div class="notification-row-main">
                <div class="notification-row-name">${escapeHtml(cfg.name)}</div>
                <div class="notification-row-meta">
                    Chat: <code>${escapeHtml(cfg.chat_id)}</code>${failureLabel}
                </div>
                <div style="margin-top:4px;">${topicChips}</div>
            </div>
            <div class="notification-row-actions">
                <span class="schedule-enabled-chip ${enabledClass}" data-action="toggle-notification" data-id="${escapeHtml(cfg.id)}">${enabledLabel}</span>
                <button class="btn tonal" data-action="test-notification" data-id="${escapeHtml(cfg.id)}">Test</button>
                <button class="btn tonal" data-action="edit-notification" data-id="${escapeHtml(cfg.id)}">Edit</button>
                <button class="btn danger" data-action="delete-notification" data-id="${escapeHtml(cfg.id)}">Delete</button>
            </div>
        </div>`;
    }).join('');
}

async function openNotificationForm(cfgId = null) {
    const content = document.getElementById('settingsContent');
    content.innerHTML = `<div class="placeholder-text">Loading…</div>`;

    let cfg = null;
    if (cfgId) {
        try {
            const res = await fetch(`${API_BASE}/notifications`, { headers: { 'Authorization': `Bearer ${sessionId}` } });
            const data = await res.json();
            cfg = (data.notifications || []).find(c => c.id === cfgId) || null;
        } catch (e) { /* ignore */ }
    }

    const selectedTopics = new Set(cfg?.topics || []);
    const topicsHtml = NOTIFICATION_TOPICS.map(t => `
        <label class="notification-topic-item">
            <input type="checkbox" data-topic="${t.id}" ${selectedTopics.has(t.id) ? 'checked' : ''}>
            <span title="${escapeHtml(t.desc)}">${escapeHtml(t.label)}</span>
        </label>`).join('');

    content.innerHTML = `
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px;">
            <button class="btn tonal" onclick="showSettingsSection('notifications')">← Back</button>
            <h2 class="settings-section-title" style="margin:0;">${cfgId ? 'Edit Notification' : 'New Notification'}</h2>
        </div>
        <div class="notification-form">
            <label>Name
                <input type="text" id="nfName" value="${escapeHtml(cfg?.name || '')}" maxlength="255" pattern="${VOLUME_NAME_PATTERN}" title="${VOLUME_NAME_TITLE}" placeholder="e.g. Homelab_Alerts">
            </label>
            <label>Bot Token
                <input type="password" id="nfToken" value="${escapeHtml(cfg?.token || '')}" maxlength="1024" placeholder="1234567890:ABC...">
                <span class="field-hint">Create a bot via @BotFather on Telegram.</span>
            </label>
            <label>Chat ID
                <input type="text" id="nfChatId" value="${escapeHtml(cfg?.chat_id || '')}" maxlength="64" pattern="-?\\d+|@\\w{4,}" title="A numeric chat id (optionally negative) or @username" placeholder="-1001234567890">
                <span class="field-hint">Your chat or group ID. Use @userinfobot to find it.</span>
            </label>
            <label>Message Thread ID <span style="font-weight:400;font-size:11px;color:var(--md-on-surface-variant)">(optional — for topic groups)</span>
                <input type="text" id="nfThreadId" value="${escapeHtml(cfg?.message_thread_id || '')}" inputmode="numeric" pattern="\\d*" maxlength="32" title="Numeric thread id" placeholder="">
            </label>
            <label>Notification Topics
                <div class="notification-topics" id="nfTopics">${topicsHtml}</div>
            </label>
            <label style="flex-direction:row;align-items:center;gap:10px;cursor:pointer;">
                <input type="checkbox" id="nfFailureOnly" ${cfg?.on_failure_only ? 'checked' : ''} style="width:15px;height:15px;">
                <span>Notify on failed tasks only</span>
            </label>
            <label>Server URL <span style="font-weight:400;font-size:11px;color:var(--md-on-surface-variant)">(optional — for self-hosted Bot API)</span>
                <input type="url" id="nfServerUrl" value="${escapeHtml(cfg?.server_url || '')}" maxlength="4096" placeholder="https://api.telegram.org">
            </label>
            <label>Message Template <span style="font-weight:400;font-size:11px;color:var(--md-on-surface-variant)">(optional — leave blank for default)</span>
                <textarea id="nfTemplate" maxlength="4096" placeholder="${escapeHtml(NOTIFICATION_DEFAULT_TEMPLATE)}">${escapeHtml(cfg?.message_template || '')}</textarea>
                <span class="field-hint">Variables: {task_type_label} {task_type} {status} {status_emoji} {target} {params_block} {elapsed} {started_at} {timestamp} {current_operation} {error} {error_block} {task_id} {hostname} — and individual param aliases: {volume} {pool} {source_volume} {source_pool} {dest_volume} {dest_pool} {backup_pool} {backup_file} {job_name}</span>
            </label>
            <div class="notification-form-actions">
                <button class="btn success" data-action="save-notification"${cfgId ? ` data-id="${escapeHtml(cfgId)}"` : ''}>Save</button>
                <button class="btn tonal" onclick="showSettingsSection('notifications')">Cancel</button>
            </div>
        </div>`;
}

async function saveNotification(cfgId) {
    const name = document.getElementById('nfName')?.value.trim();
    const token = document.getElementById('nfToken')?.value.trim();
    const chatId = document.getElementById('nfChatId')?.value.trim();
    const threadId = document.getElementById('nfThreadId')?.value.trim() || null;
    const failureOnly = document.getElementById('nfFailureOnly')?.checked || false;
    const serverUrl = document.getElementById('nfServerUrl')?.value.trim() || 'https://api.telegram.org';
    const template = document.getElementById('nfTemplate')?.value.trim() || null;

    if (!name) { showError('Name is required'); return; }
    if (!token) { showError('Bot token is required'); return; }
    if (!chatId) { showError('Chat ID is required'); return; }

    const topics = [];
    document.querySelectorAll('#nfTopics input[type="checkbox"]:checked').forEach(cb => {
        topics.push(cb.dataset.topic);
    });
    if (!topics.length) { showError('Select at least one notification topic'); return; }

    const body = {
        name,
        token,
        chat_id: chatId,
        message_thread_id: threadId,
        topics,
        on_failure_only: failureOnly,
        server_url: serverUrl,
        message_template: template,
    };

    const url = cfgId ? `${API_BASE}/notifications/${cfgId}` : `${API_BASE}/notifications`;
    const method = cfgId ? 'PUT' : 'POST';

    try {
        const res = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${sessionId}` },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (res.ok) {
            showSuccess(cfgId ? 'Notification updated' : 'Notification created');
            showSettingsSection('notifications');
        } else {
            showError(data.detail || 'Failed to save notification');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

async function deleteNotification(cfgId) {
    if (!confirm('Delete this notification configuration?')) return;
    try {
        const res = await fetch(`${API_BASE}/notifications/${cfgId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${sessionId}` },
        });
        if (res.ok) {
            showSuccess('Notification deleted');
            loadNotifications();
        } else {
            const data = await res.json();
            showError(data.detail || 'Failed to delete notification');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

async function toggleNotification(cfgId) {
    try {
        const res = await fetch(`${API_BASE}/notifications/${cfgId}/toggle`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${sessionId}` },
        });
        if (res.ok) {
            loadNotifications();
        } else {
            const data = await res.json();
            showError(data.detail || 'Failed to toggle notification');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

async function testNotification(cfgId) {
    try {
        const res = await fetch(`${API_BASE}/notifications/${cfgId}/test`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${sessionId}` },
        });
        if (res.ok) {
            showSuccess('Test message sent — check your Telegram');
        } else {
            const data = await res.json();
            showError(data.detail || 'Test message failed');
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
                        maxlength="255" pattern="${VOLUME_NAME_PATTERN}" title="${VOLUME_NAME_TITLE}"
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
let _taskDetailStack = [];

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
    job_name:          'Schedule',
    parent_job:        'Schedule',
    total_volumes:     'Volumes',
    scheduled:         'Scheduled',
};

// Internal correlation fields — not useful to show in the params table.
const HIDDEN_PARAM_KEYS = new Set(['parent_task_id', 'job_id']);

function openTaskDetailModalById(taskId) {
    const task = taskHistory.find(t => t.task_id === taskId);
    if (task) openTaskDetailModal(task);
}

// Drill into a sub-task from an already-open detail view, remembering where we
// came from so "← Back" can return to the parent (e.g. the Scheduled task).
function navigateToTaskDetail(taskId) {
    if (_taskDetailCurrentId && _taskDetailCurrentId !== taskId) {
        _taskDetailStack.push(_taskDetailCurrentId);
    }
    openTaskDetailModalById(taskId);
}

function taskDetailBack() {
    const prev = _taskDetailStack.pop();
    if (prev) openTaskDetailModalById(prev);
}

function openTaskDetailModal(task) {
    // Drop any poll from a previously-open detail before we repurpose the modal.
    if (_taskDetailPollInterval) {
        clearInterval(_taskDetailPollInterval);
        _taskDetailPollInterval = null;
    }
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
    _taskDetailStack = [];
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

            if (prog.status === 'completed' || prog.status === 'failed' || prog.status === 'cancelled') {
                if (_taskDetailPollInterval) {
                    clearInterval(_taskDetailPollInterval);
                    _taskDetailPollInterval = null;
                }
            }
        }
    } catch (e) { /* network error — silently skip */ }
}

async function cancelTask(taskId) {
    if (!taskId) return;
    if (!confirm('Cancel this task? A running operation will be stopped and any partial data cleaned up.')) return;
    try {
        const res = await fetch(`${API_BASE}/task/${taskId}/cancel`, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
            showSuccess(data.status === 'cancelled' ? 'Task cancelled' : 'Cancelling task…');
            _refreshTaskDetail(taskId);
        } else {
            showError(data.detail || 'Failed to cancel task');
        }
    } catch (e) {
        showError(`Error: ${e.message}`);
    }
}

function _renderTaskDetail(task, logLines) {
    if (_taskDetailCurrentId !== task.task_id) return;

    document.getElementById('taskDetailTitle').textContent =
        (task.task_type || 'Task').toUpperCase();

    const params = task.params || {};
    const paramRows = Object.entries(params)
        .filter(([k, v]) => v !== null && v !== undefined && v !== '' && !HIDDEN_PARAM_KEYS.has(k))
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

    const backBtn = _taskDetailStack.length
        ? `<button class="btn tonal task-detail-back-btn" data-action="task-detail-back">← Back</button>`
        : '';

    // For a "Scheduled" run or a bulk action, list the per-item sub-tasks it
    // spawned. Each is its own task and opens its own detail view (with a back
    // link to here).
    let subTaskSection = '';
    const isGroupTask = task.task_type === 'scheduled_backup' || (task.task_type || '').startsWith('bulk_');
    if (isGroupTask) {
        const sectionTitle = task.task_type === 'scheduled_backup' ? 'Volume Backups' : 'Items';
        const subTasks = taskHistory.filter(t => t.params && t.params.parent_task_id === task.task_id);
        const subTaskHtml = subTasks.length
            ? subTasks.map(st => `
                <div class="subtask-item" data-action="nav-task-detail" data-id="${escapeHtml(st.task_id)}" title="Click for details">
                    <div class="subtask-main">
                        <span class="task-status ${st.status}">${st.status.toUpperCase()}</span>
                        <span class="subtask-target">${escapeHtml(getTaskTargetLabel(st))}</span>
                    </div>
                    <div class="subtask-meta">${st.progress_percent || 0}%${st.error ? ' · failed' : ''}</div>
                </div>`).join('')
            : '<div class="log-empty">No items recorded for this run yet.</div>';
        subTaskSection = `
            <div class="task-detail-section">
                <div class="task-detail-section-title">${sectionTitle} (${subTasks.length})</div>
                <div class="subtask-list">${subTaskHtml}</div>
            </div>`;
    }

    document.getElementById('taskDetailBody').innerHTML = `
        ${backBtn}
        <div class="task-detail-header">
            <span class="task-type-pill type-${(task.task_type || 'operation').replace(/[^a-z0-9_]/gi, '_')}">${getTaskTypeDisplay(task.task_type)}</span>
            <span class="task-status ${task.status}">${task.status.toUpperCase()}</span>
            <span class="task-detail-target">${escapeHtml(getTaskTargetLabel(task))}</span>
            ${(task.status === 'running' || task.status === 'pending')
                ? `<button class="btn danger task-cancel-btn" data-action="cancel-task" data-id="${escapeHtml(task.task_id)}">Cancel</button>`
                : ''}
        </div>

        <div class="task-detail-progress">
            <div class="progress-bar">
                <div class="progress-fill" style="width: ${task.progress_percent || 0}%"></div>
            </div>
            <div class="task-detail-stats">
                <span>${task.progress_percent || 0}%</span>
                <span>${formatDuration(task.elapsed_seconds)} elapsed</span>
                ${task.estimated_remaining_seconds ? `<span>~${formatDuration(task.estimated_remaining_seconds)} left</span>` : ''}
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

        ${subTaskSection}

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
// ── Delegated action dispatch ───────────────────────────────────────────────
// Markup uses data-action + data-* attributes instead of inline onclick="fn('...')"
// so user-controlled names (pools, volumes, files) can never break out of an
// attribute into executable JS. Values are read safely via element.dataset.
const ACTION_HANDLERS = {
    'select-pool':         (d) => selectPool(d.pool),
    'open-create-volume':  (d) => openCreateVolumeModal(d.pool),
    'open-migrate':        (d) => openMigrateModal(d.pool, d.vol),
    'open-backup':         (d) => openBackupModal(d.pool, d.vol),
    'open-rename':         (d) => openRenameVolumeModal(d.pool, d.vol),
    'open-permissions':    (d) => openPermissionsModal(d.pool, d.vol),
    'open-delete':         (d) => openDeleteModal(d.pool, d.vol),
    'open-restore':        (d) => openRestoreModal(d.pool, d.file),
    'create-volume':       (d) => createVolume(d.pool),
    'rename-volume':       (d) => renameVolume(d.pool, d.vol),
    'save-permissions':    (d) => savePermissions(d.pool, d.vol),
    'start-migration':     (d) => startMigration(d.pool, d.vol),
    'start-backup':        (d) => startBackup(d.pool, d.vol),
    'start-restore':       (d) => startRestore(d.pool, d.file),
    'confirm-delete':      (d) => confirmDelete(d.pool, d.vol),
    'bulk-clear':          ()  => clearBulkSelection(),
    'bulk-select-all':     ()  => selectAllBulk(),
    'bulk-open-backup':    ()  => openBulkBackupModal(),
    'bulk-open-migrate':   ()  => openBulkMigrateModal(),
    'bulk-open-permissions': () => openBulkPermissionsModal(),
    'bulk-open-delete':    ()  => openBulkDeleteModal(),
    'bulk-open-restore':   ()  => openBulkRestoreModal(),
    'start-bulk-backup':   ()  => startBulkBackup(),
    'start-bulk-migrate':  ()  => startBulkMigrate(),
    'start-bulk-permissions': () => startBulkPermissions(),
    'confirm-bulk-delete': ()  => confirmBulkDelete(),
    'start-bulk-restore':  ()  => startBulkRestore(),
    'toggle-schedule':     (d) => toggleSchedule(d.id),
    'run-schedule':        (d) => runScheduleNow(d.id),
    'edit-schedule':       (d) => openScheduleForm(d.id),
    'delete-schedule':     (d) => deleteSchedule(d.id),
    'save-schedule':       (d) => saveSchedule(d.id ?? null),
    'toggle-notification': (d) => toggleNotification(d.id),
    'test-notification':   (d) => testNotification(d.id),
    'edit-notification':   (d) => openNotificationForm(d.id),
    'delete-notification': (d) => deleteNotification(d.id),
    'save-notification':   (d) => saveNotification(d.id ?? null),
    'open-task-detail':    (d) => openTaskDetailModalById(d.id),
    'nav-task-detail':     (d) => navigateToTaskDetail(d.id),
    'task-detail-back':    ()  => taskDetailBack(),
    'cancel-task':         (d) => cancelTask(d.id),
    'toggle-container-tooltip': (d, el) => {
        if (!el) return;
        const willPin = !el.classList.contains('pinned');
        document.querySelectorAll('.container-badge.pinned').forEach(b => {
            if (b !== el) { b.classList.remove('pinned'); hideContainerTip(b); }
        });
        el.classList.toggle('pinned', willPin);
        if (willPin) positionContainerTip(el); else hideContainerTip(el);
    },
};

document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    // Close any pinned container tooltip when clicking outside its badge.
    document.querySelectorAll('.container-badge.pinned').forEach(b => {
        if (!b.contains(e.target)) { b.classList.remove('pinned'); hideContainerTip(b); }
    });
    if (!el) return;
    const handler = ACTION_HANDLERS[el.dataset.action];
    if (handler) handler(el.dataset, el);
});

// Container badge tooltip: show on hover/focus, pin open on click/tap (for touch).
document.addEventListener('mouseover', (e) => {
    const b = e.target.closest && e.target.closest('.container-badge');
    if (b) positionContainerTip(b);
});
document.addEventListener('mouseout', (e) => {
    const b = e.target.closest && e.target.closest('.container-badge');
    if (b && !b.classList.contains('pinned') && !b.contains(e.relatedTarget)) hideContainerTip(b);
});
document.addEventListener('focusin', (e) => {
    const b = e.target.closest && e.target.closest('.container-badge');
    if (b) positionContainerTip(b);
});
document.addEventListener('focusout', (e) => {
    const b = e.target.closest && e.target.closest('.container-badge');
    if (b && !b.classList.contains('pinned') && !b.contains(e.relatedTarget)) hideContainerTip(b);
});

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
