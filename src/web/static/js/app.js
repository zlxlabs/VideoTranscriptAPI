/**
 * 视频转录Web应用主要JavaScript文件
 * 负责URL提取、本地存储、API调用等核心功能
 */

// 应用配置
const APP_CONFIG = {
    STORAGE_KEYS: {
        BEARER_TOKEN: 'vta_bearer_token',
        WECHAT_WEBHOOK: 'vta_wechat_webhook',
        SPEAKER_RECOGNITION: 'vta_speaker_recognition',
        TASK_HISTORY: 'vta_task_history',
        THEME_PREFERENCE: 'vta_theme_preference'
    },
    API_BASE_URL: '',
    MAX_HISTORY_ITEMS: 10
};

/** Stable user-visible failure text used when the shared auth script is absent. */
const AUTH_STORAGE_ERROR_MESSAGE = '安全错误：统一鉴权模块加载失败，已禁用受保护操作';
let authStorageErrorShown = false;

// 全局变量
let currentTask = null;
let isAdvancedSettingsExpanded = false;
let webhookSaveTimer = null;
let currentMode = 'url';
let selectedUploadFile = null;
let uploadCapabilities = null;
let currentUploadIntent = null;
let isUploading = false;

/**
 * 通用URL提取正则表达式
 */
const URL_PATTERNS = [
    // 标准HTTP/HTTPS URL
    /https?:\/\/[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+/gi,
    // 支持无协议的URL（如 www.example.com）
    /(?:www\.)[a-zA-Z0-9][-a-zA-Z0-9]*[a-zA-Z0-9]*\.[a-zA-Z]{2,}[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]*/gi,
    // 支持短链（如 t.co, bit.ly 等）
    /[a-zA-Z0-9][-a-zA-Z0-9]*[a-zA-Z0-9]*\.[a-zA-Z]{2,}\/[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+/gi
];

/**
 * Resolve the shared auth module loaded before app.js; no legacy token path is
 * allowed when the shared script is missing or malformed.
 */
function getAuthStorage() {
    const candidate = (typeof window !== 'undefined' && window.VideoTranscriptAuthStorage) ||
        (typeof globalThis !== 'undefined' && globalThis.VideoTranscriptAuthStorage);
    if (!candidate || typeof candidate.readAuthToken !== 'function' ||
        typeof candidate.writeAuthToken !== 'function' ||
        typeof candidate.clearAuthToken !== 'function' ||
        typeof candidate.buildAuthHeaders !== 'function') {
        return null;
    }
    return candidate;
}

/**
 * Disable protected submission and surface a stable safety error when the
 * shared auth script failed to load; token text is never included.
 */
function disableProtectedActions() {
    const submitButton = typeof document !== 'undefined' && document.getElementById('submit-btn');
    const tokenInput = typeof document !== 'undefined' && document.getElementById('bearer-token');
    if (submitButton) submitButton.disabled = true;
    if (tokenInput) tokenInput.disabled = true;
    if (!authStorageErrorShown && typeof UIManager !== 'undefined' &&
        typeof document !== 'undefined' && document.getElementById('status-container')) {
        authStorageErrorShown = true;
        UIManager.showStatus('error', AUTH_STORAGE_ERROR_MESSAGE, '请刷新页面后重试');
    }
}

/**
 * Report a missing auth module at the protected-operation boundary and return
 * null so callers cannot silently fall back to the old localStorage token.
 */
function requireAuthStorage() {
    const authStorage = getAuthStorage();
    if (!authStorage) {
        disableProtectedActions();
        return null;
    }
    return authStorage;
}

/** Sync the homepage token field after canonical or legacy shared-storage events. */
function handleHomepageAuthStorageEvent(event) {
    const authStorage = getAuthStorage();
    const storageKeys = authStorage && authStorage.AUTH_STORAGE_KEYS;
    if (!storageKeys || ![
        storageKeys.canonical,
        storageKeys.migration,
        storageKeys.legacyApi,
        storageKeys.legacyPersistent,
        storageKeys.legacySession,
    ].includes(event && event.key)) return;
    const tokenInput = document.getElementById('bearer-token');
    if (tokenInput) tokenInput.value = authStorage.readAuthToken() || '';
    UIManager.updateSubmitButton();
}

/**
 * HTML 转义（属性/元素上下文通用）：& < > " '
 * 提取出的 URL/标题插入 innerHTML 前必须过此函数（Codex R6-1：系统分享
 * 可把带 <>/引号 的 URL 投进预览，不转义即同源 XSS）
 */
function escapeHTML(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/**
 * 构建单条历史记录的 HTML（纯函数，可 vitest）。
 * title / original_text / url / id / view_token 均为第三方可控字段
 * （E4 分享预填的原文、平台标题等），一律过 escapeHTML（Codex R8-1）；
 * 按钮经 data-* 传值、由 renderHistory 用 addEventListener 绑定，
 * 不用内联 onclick 的单引号属性上下文。
 */
function buildHistoryItemHTML(task) {
    const timeStr = escapeHTML(new Date(task.timestamp).toLocaleString('zh-CN'));
    const originalTextPreview = task.original_text ?
        (task.original_text.length > 100 ? task.original_text.substring(0, 100) + '...' : task.original_text) : '';
    const isUpload = task.source === 'upload';
    const retentionBadge = task.retention ? `<span class="feature-tag">• 保留: ${escapeHTML(task.retention)}</span>` : '';
    return `
        <div class="history-info">
            <div class="history-title">${escapeHTML(task.title)}</div>
            ${originalTextPreview ? `
                <div class="history-original-text">
                    <span class="original-text-label">${isUpload ? '来源链接：' : '原始内容：'}</span>
                    <span class="original-text-content">${escapeHTML(originalTextPreview)}</span>
                </div>
            ` : ''}
            ${task.url ? `<div class="history-url">${escapeHTML(task.url)}</div>` : ''}
            <div class="history-meta">
                <span>${timeStr}</span>
                ${isUpload ? '<span class="feature-tag">• 本地上传</span>' : ''}
                ${task.useSpeakerRecognition ? '<span class="feature-tag">• 说话人识别</span>' : ''}
                ${retentionBadge}
            </div>
        </div>
        <div class="history-actions">
            ${task.url ? `<button class="history-btn history-copy-btn" data-url="${escapeHTML(task.url)}">📋 复制</button>` : ''}
            <a class="history-btn" href="/view/${escapeHTML(task.view_token || task.id)}" target="_blank">👁️ 查看</a>
            <button class="history-btn delete-btn history-delete-btn" data-task-id="${escapeHTML(task.id)}">🗑️ 删除</button>
        </div>
    `;
}

/**
 * 本地存储管理类
 */
class StorageManager {
    static set(key, value) {
        if (key === APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN) {
            const authStorage = requireAuthStorage();
            if (!authStorage) return false;
            if (!value) return authStorage.clearAuthToken();
            return authStorage.writeAuthToken(value, { remember: true });
        }
        try {
            if (key === APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK) {
                // Webhook 地址直接存储（不是秘密）
                localStorage.setItem(key, value);
            } else {
                localStorage.setItem(key, JSON.stringify(value));
            }
        } catch (e) {
            console.error('存储失败:', e);
        }
    }

    static get(key) {
        if (key === APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN) {
            const authStorage = requireAuthStorage();
            return authStorage ? authStorage.readAuthToken() : null;
        }
        try {
            const value = localStorage.getItem(key);
            if (!value) return null;

            if (key === APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK) {
                // Webhook 地址直接读取
                return value;
            } else {
                return JSON.parse(value);
            }
        } catch (e) {
            console.error('读取存储失败:', e);
            return null;
        }
    }

    static remove(key) {
        if (key === APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN) {
            const authStorage = requireAuthStorage();
            return authStorage ? authStorage.clearAuthToken() : false;
        }
        try {
            localStorage.removeItem(key);
        } catch (e) {
            console.error('删除存储失败:', e);
        }
    }

    static clear() {
        this.remove(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
        try {
            Object.values(APP_CONFIG.STORAGE_KEYS)
                .filter(key => key !== APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN)
                .forEach(key => {
                    localStorage.removeItem(key);
            });
        } catch (e) {
            console.error('清空存储失败:', e);
        }
    }
}

/**
 * URL提取和处理工具类
 */
class URLExtractor {
    /**
     * 从文本中提取所有URL
     */
    static extractURLs(text) {
        const urls = [];
        const seenUrls = new Set();

        URL_PATTERNS.forEach(pattern => {
            const matches = text.match(pattern);
            if (matches) {
                matches.forEach(url => {
                    const cleanUrl = this.cleanURL(url);
                    if (cleanUrl && !seenUrls.has(cleanUrl)) {
                        seenUrls.add(cleanUrl);
                        urls.push(cleanUrl);
                    }
                });
            }
        });

        return urls;
    }

    /**
     * 清理URL（移除末尾标点符号，确保协议前缀等）
     */
    static cleanURL(url) {
        if (!url) return null;

        // 移除末尾的标点符号和特殊字符
        url = url.replace(/[.,;:!?)\]}>'"。，；：！？）】》'"]+$/, '');

        // 确保有协议前缀
        if (!url.match(/^https?:\/\//)) {
            url = 'https://' + url;
        }

        // 基本URL格式验证
        try {
            new URL(url);
            return url;
        } catch (e) {
            return null;
        }
    }

    /**
     * URL评分系统，优先显示最可能的视频链接
     */
    static scoreURL(url) {
        let score = 0;

        // 已知视频平台域名加分
        const videoDomains = [
            'youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv',
            'xiaohongshu.com', 'xhslink.com', 'xhslink.cn', 'douyin.com', 'v.douyin.com',
            'xiaoyuzhoufm.com', 'tiktok.com', 'vm.tiktok.com',
            'weixin.qq.com'
        ];

        if (videoDomains.some(domain => url.includes(domain))) {
            score += 10;
        }

        // 短链服务域名加分
        const shortLinkDomains = [
            't.co', 'bit.ly', 'tinyurl.com', 'short.link',
            'suo.im', 'dwz.cn', 'urlc.cn'
        ];

        if (shortLinkDomains.some(domain => url.includes(domain))) {
            score += 5;
        }

        // URL包含视频相关关键词加分
        const videoKeywords = ['video', 'watch', 'v', 'play', 'episode'];
        if (videoKeywords.some(keyword => url.toLowerCase().includes(keyword))) {
            score += 3;
        }

        // 更长的路径通常是内容页面
        const pathLength = url.split('/').length;
        if (pathLength > 3) {
            score += pathLength - 3;
        }

        return score;
    }

    /**
     * 智能URL提取和排序
     */
    static extractAndRankURLs(text) {
        const urls = this.extractURLs(text);

        return urls.map(url => ({
            url: url,
            score: this.scoreURL(url),
            display: url.length > 50 ? url.substring(0, 47) + '...' : url
        })).sort((a, b) => b.score - a.score);
    }
}

/**
 * API调用管理类
 */
class APIManager {
    /**
     * 提交转录任务
     */
    static async submitTranscription(url, useSpeakerRecognition, wechatWebhook = null) {
        const authStorage = requireAuthStorage();
        const token = authStorage && authStorage.readAuthToken();

        if (!token) {
            throw new Error(authStorage ? '请先设置访问令牌' : AUTH_STORAGE_ERROR_MESSAGE);
        }

        const requestBody = {
            url: url,
            use_speaker_recognition: useSpeakerRecognition
        };

        // 只有当 webhook 不为空时才添加到请求体中
        if (wechatWebhook && wechatWebhook.trim() !== '') {
            requestBody.wechat_webhook = wechatWebhook.trim();
        }

        const response = await fetch('/api/transcribe', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...authStorage.buildAuthHeaders()
            },
            body: JSON.stringify(requestBody)
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ message: '请求失败' }));
            throw new Error(errorData.message || `HTTP ${response.status}`);
        }

        return await response.json();
    }

    /**
     * 查询任务状态
     */
    static async getTaskStatus(taskId) {
        const authStorage = requireAuthStorage();
        const token = authStorage && authStorage.readAuthToken();

        if (!token) {
            throw new Error(authStorage ? '请先设置访问令牌' : AUTH_STORAGE_ERROR_MESSAGE);
        }

        const response = await fetch(`/api/task/${taskId}`, {
            headers: authStorage.buildAuthHeaders()
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ message: '查询失败' }));
            throw new Error(errorData.message || `HTTP ${response.status}`);
        }

        return await response.json();
    }

    /**
     * 获取上传功能配置
     */
    static async getUploadCapabilities() {
        const authStorage = requireAuthStorage();
        const headers = authStorage ? authStorage.buildAuthHeaders() : {};
        const response = await fetch('/api/uploads/capabilities', {
            headers
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.message || errorData.detail || `HTTP ${response.status}`);
        }

        return await response.json();
    }

    /**
     * 提交本地音视频上传
     */
    static async submitUpload(file, idempotencyKey, metadata) {
        const authStorage = requireAuthStorage();
        const token = authStorage && authStorage.readAuthToken();

        if (!token) {
            throw new Error(authStorage ? '请先设置访问令牌' : AUTH_STORAGE_ERROR_MESSAGE);
        }

        const encodedMetadata = encodeUploadMetadata(metadata);
        const headers = {
            'Content-Type': 'application/octet-stream',
            'Idempotency-Key': idempotencyKey,
            'X-Upload-Metadata': encodedMetadata,
            ...authStorage.buildAuthHeaders()
        };

        const response = await fetch('/api/uploads', {
            method: 'POST',
            headers,
            body: file
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            const error = new Error(errorData.message || errorData.detail || `HTTP ${response.status}`);
            error.status = response.status;
            error.data = errorData;
            throw error;
        }

        return await response.json();
    }

    /**
     * 根据 Idempotency-Key 查询上传回执
     */
    static async getUploadReceipt(idempotencyKey) {
        const authStorage = requireAuthStorage();
        const headers = authStorage ? authStorage.buildAuthHeaders() : {};
        const response = await fetch(`/api/uploads/by-idempotency-key/${encodeURIComponent(idempotencyKey)}`, {
            headers
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            const error = new Error(errorData.message || errorData.detail || `HTTP ${response.status}`);
            error.status = response.status;
            throw error;
        }

        return await response.json();
    }

    /**
     * 停止上传内容的公开分享
     */
    static async stopUploadShare(uploadId) {
        const authStorage = requireAuthStorage();
        const token = authStorage && authStorage.readAuthToken();

        if (!token) {
            throw new Error(authStorage ? '请先设置访问令牌' : AUTH_STORAGE_ERROR_MESSAGE);
        }

        const response = await fetch(`/api/uploads/${encodeURIComponent(uploadId)}/share`, {
            method: 'DELETE',
            headers: authStorage.buildAuthHeaders()
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            const error = new Error(errorData.message || errorData.detail || `HTTP ${response.status}`);
            error.status = response.status;
            throw error;
        }

        return await response.json();
    }
}

/**
 * 任务历史管理类
 */
class TaskHistoryManager {
    /**
     * 添加任务到历史记录
     * @param {Object} taskData 任务数据
     * @returns {Object} 包含是否为重复任务的信息
     */
    static addTask(taskData) {
        try {
            const history = this.getHistory();
            const newTask = {
                id: taskData.task_id,
                view_token: taskData.view_token,
                upload_id: taskData.upload_id || null,
                source: taskData.source || 'url',
                url: taskData.url || '',
                original_text: taskData.original_text || '',
                title: taskData.title || (taskData.url ? this.extractTitleFromURL(taskData.url) : '本地上传'),
                timestamp: Date.now(),
                useSpeakerRecognition: taskData.use_speaker_recognition || false,
                retention: taskData.retention || null,
                share_active: taskData.share_active !== undefined ? taskData.share_active : true,
                status: 'submitted'
            };

            // 基于URL去重：仅当URL非空时相同URL才去重保留最新
            const existingUrlIndex = newTask.url ? history.findIndex(task => task.url === newTask.url) : -1;
            let isDuplicate = false;
            let oldTask = null;
            
            if (existingUrlIndex !== -1) {
                // 如果已存在相同URL的任务，移除旧的记录
                oldTask = history[existingUrlIndex];
                history.splice(existingUrlIndex, 1);
                isDuplicate = true;
                console.log(`检测到重复URL，已移除旧记录: ${newTask.url}`);
            }
            
            // 将新任务添加到最前面
            history.unshift(newTask);

            // 保持历史记录数量限制
            if (history.length > APP_CONFIG.MAX_HISTORY_ITEMS) {
                history.splice(APP_CONFIG.MAX_HISTORY_ITEMS);
            }

            StorageManager.set(APP_CONFIG.STORAGE_KEYS.TASK_HISTORY, history);
            this.renderHistory();
            
            return {
                isDuplicate: isDuplicate,
                oldTask: oldTask,
                newTask: newTask
            };
        } catch (e) {
            console.error('添加任务历史失败:', e);
            return { isDuplicate: false, error: e.message };
        }
    }

    /**
     * 获取任务历史记录
     */
    static getHistory() {
        return StorageManager.get(APP_CONFIG.STORAGE_KEYS.TASK_HISTORY) || [];
    }

    /**
     * 删除指定任务
     */
    static deleteTask(taskId) {
        try {
            if (!confirm('确定要删除这个任务记录吗？')) {
                return;
            }
            
            const history = this.getHistory();
            const updatedHistory = history.filter(task => task.id !== taskId);
            
            StorageManager.set(APP_CONFIG.STORAGE_KEYS.TASK_HISTORY, updatedHistory);
            this.renderHistory();
            
            UIManager.showStatus('success', '任务记录已删除');
            setTimeout(UIManager.hideStatus, 2000);
        } catch (e) {
            console.error('删除任务记录失败:', e);
            UIManager.showStatus('error', '删除失败', '请稍后重试');
            setTimeout(UIManager.hideStatus, 3000);
        }
    }

    /**
     * 从URL提取简单标题
     */
    static extractTitleFromURL(url) {
        try {
            const urlObj = new URL(url);
            const hostname = urlObj.hostname.replace('www.', '');
            
            if (hostname.includes('youtube.com') || hostname.includes('youtu.be')) {
                return 'YouTube视频';
            } else if (hostname.includes('bilibili.com')) {
                return 'Bilibili视频';
            } else if (hostname.includes('xiaohongshu.com') || hostname.includes('xhslink.com') || hostname.includes('xhslink.cn')) {
                return '小红书内容';
            } else if (hostname.includes('douyin.com')) {
                return '抖音视频';
            } else if (hostname.includes('xiaoyuzhoufm.com')) {
                return '小宇宙播客';
            } else if (hostname.includes('weixin.qq.com')) {
                return '视频号视频';
            } else if (hostname.includes('twitter.com') || hostname === 'x.com' || hostname.endsWith('.x.com')) {
                return 'X视频';
            } else {
                return '视频内容';
            }
        } catch (e) {
            return '视频内容';
        }
    }

    /**
     * 渲染历史记录
     */
    static renderHistory() {
        const history = this.getHistory();
        const container = document.getElementById('history-container');
        const list = document.getElementById('history-list');

        if (history.length === 0) {
            container.style.display = 'none';
            return;
        }

        container.style.display = 'block';
        list.innerHTML = '';

        history.forEach((task, index) => {
            const item = document.createElement('div');
            item.className = 'history-item fade-in';

            // HTML 由纯函数构建（全字段转义，Codex R8-1）；
            // 按钮经 data-* 传值 + addEventListener 绑定，无内联 onclick
            item.innerHTML = buildHistoryItemHTML(task);

            const copyBtn = item.querySelector('.history-copy-btn');
            if (copyBtn) copyBtn.addEventListener('click', () => copyToClipboard(copyBtn.dataset.url));
            const deleteBtn = item.querySelector('.history-delete-btn');
            if (deleteBtn) deleteBtn.addEventListener('click', () => TaskHistoryManager.deleteTask(deleteBtn.dataset.taskId));

            list.appendChild(item);
        });
    }
}

/**
 * 主题管理类
 */
class ThemeManager {
    /**
     * 初始化主题系统
     */
    static initialize() {
        // 获取保存的主题偏好
        const savedTheme = StorageManager.get(APP_CONFIG.STORAGE_KEYS.THEME_PREFERENCE);
        
        // 如果没有保存的主题，则检测系统偏好
        let theme = savedTheme;
        if (!theme) {
            theme = this.detectSystemTheme();
        }
        
        // 应用主题
        this.applyTheme(theme);
        
        // 绑定主题切换按钮事件
        const themeToggle = document.getElementById('theme-toggle');
        if (themeToggle) {
            themeToggle.addEventListener('click', () => this.toggleTheme());
        }
        
        // 监听系统主题变化（如果用户没有手动设置过主题）
        if (!savedTheme && window.matchMedia) {
            const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
            mediaQuery.addEventListener('change', (e) => {
                // 只在用户未手动设置主题时才自动切换
                const currentSavedTheme = StorageManager.get(APP_CONFIG.STORAGE_KEYS.THEME_PREFERENCE);
                if (!currentSavedTheme) {
                    this.applyTheme(e.matches ? 'dark' : 'light');
                }
            });
        }
    }
    
    /**
     * 检测系统主题偏好
     */
    static detectSystemTheme() {
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
            return 'light';
        }
        return 'dark';
    }
    
    /**
     * 应用主题
     */
    static applyTheme(theme) {
        const root = document.documentElement;
        const themeToggle = document.getElementById('theme-toggle');

        // 始终通过 data-theme 属性设置主题
        root.setAttribute('data-theme', theme);

        if (theme === 'dark') {
            if (themeToggle) {
                themeToggle.textContent = '☀️';
                themeToggle.title = '切换到浅色模式';
            }
        } else {
            if (themeToggle) {
                themeToggle.textContent = '🌙';
                themeToggle.title = '切换到深色模式';
            }
        }
    }
    
    /**
     * 切换主题
     */
    static toggleTheme() {
        const currentTheme = this.getCurrentTheme();
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        const themeToggle = document.getElementById('theme-toggle');
        
        // 添加按钮旋转动画
        if (themeToggle) {
            themeToggle.classList.add('switching');
            setTimeout(() => {
                themeToggle.classList.remove('switching');
            }, 600);
        }
        
        // 保存用户偏好
        StorageManager.set(APP_CONFIG.STORAGE_KEYS.THEME_PREFERENCE, newTheme);
        
        // 添加页面过渡动画
        const body = document.body;
        body.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)';
        
        // 应用新主题
        setTimeout(() => {
            this.applyTheme(newTheme);
        }, 50);
        
        // 清除过渡样式
        setTimeout(() => {
            body.style.transition = '';
        }, 350);
    }
    
    /**
     * 获取当前主题
     */
    static getCurrentTheme() {
        return document.documentElement.getAttribute('data-theme') || 'dark';
    }
}

/**
 * UI管理类
 */
class UIManager {
    /**
     * 显示状态信息
     */
    static showStatus(type, message, details = '') {
        const container = document.getElementById('status-container');
        const content = document.getElementById('status-content');
        
        container.className = `status-container status-${type} fade-in`;
        container.style.display = 'block';

        let icon = '';
        switch (type) {
            case 'success':
                icon = '✅';
                break;
            case 'error':
                icon = '❌';
                break;
            case 'loading':
                icon = '<span class="loading-spinner"></span>';
                break;
            default:
                icon = 'ℹ️';
        }

        content.innerHTML = `
            <div style="font-size: 1.1rem; font-weight: 600; margin-bottom: 0.5rem;">
                ${icon} ${message}
            </div>
            ${details ? `<div style="font-size: 0.95rem; opacity: 0.8;">${details}</div>` : ''}
        `;

        // 滚动到状态区域
        container.scrollIntoView({ behavior: 'smooth' });
    }

    /**
     * 隐藏状态信息
     */
    static hideStatus() {
        const container = document.getElementById('status-container');
        container.style.display = 'none';
    }

    /**
     * 更新提交按钮状态
     */
    static updateSubmitButton() {
        if (typeof document === 'undefined') return;
        const btn = document.getElementById('submit-btn');
        if (!btn) return;
        const btnIcon = btn.querySelector('.btn-icon');
        const btnText = btn.querySelector('.btn-text');

        if (!getAuthStorage()) {
            disableProtectedActions();
            btn.disabled = true;
            btnIcon.textContent = '🔒';
            btnText.textContent = '鉴权模块不可用，已禁用提交';
            return;
        }

        const token = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
        const authPrompt = document.getElementById('auth-missing-prompt');

        if (authPrompt) {
            authPrompt.hidden = Boolean(token);
        }

        if (currentMode === 'upload') {
            let fileValid = Boolean(selectedUploadFile);
            if (selectedUploadFile && uploadCapabilities && uploadCapabilities.limits && uploadCapabilities.limits.max_file_mib) {
                if (selectedUploadFile.size > uploadCapabilities.limits.max_file_mib * 1024 * 1024) {
                    fileValid = false;
                }
            }
            const uploadEnabled = uploadCapabilities ? (uploadCapabilities.enabled !== false) : true;
            const canSubmit = fileValid && token && !isUploading && uploadEnabled;

            btn.disabled = !canSubmit;

            if (isUploading) {
                btnIcon.textContent = '⏳';
                btnText.textContent = '上传中...';
            } else if (!selectedUploadFile) {
                btnIcon.textContent = '📁';
                btnText.textContent = '开始上传';
            } else {
                btnIcon.textContent = '🚀';
                btnText.textContent = '开始上传';
            }
            return;
        }

        const selectedURL = getSelectedURL();
        const canSubmit = selectedURL && token && !currentTask;
        
        btn.disabled = !canSubmit;
        
        if (currentTask) {
            btnIcon.textContent = '⏳';
            btnText.textContent = '处理中...';
        } else if (!selectedURL) {
            btnIcon.textContent = '📝';
            btnText.textContent = '开始转录';
        } else {
            btnIcon.textContent = '🚀';
            btnText.textContent = '开始转录';
        }
    }

    /**
     * 切换高级设置显示状态
     */
    static toggleAdvancedSettings() {
        const settings = document.getElementById('advanced-settings');
        const toggle = document.getElementById('advanced-toggle');
        const icon = toggle && toggle.querySelector('.toggle-icon');
        if (!settings || !toggle) return;

        isAdvancedSettingsExpanded = !isAdvancedSettingsExpanded;

        if (isAdvancedSettingsExpanded) {
            settings.classList.add('expanded');
            settings.hidden = false;
            toggle.setAttribute('aria-expanded', 'true');
            if (icon) icon.classList.add('rotated');
        } else {
            settings.classList.remove('expanded');
            settings.hidden = true;
            toggle.setAttribute('aria-expanded', 'false');
            if (icon) icon.classList.remove('rotated');
        }
    }

    /** Toggle speaker recognition, a high-frequency option kept before the CTA. */
    static toggleTranscriptionOptions() {
        const options = document.getElementById('transcription-options');
        const toggle = document.getElementById('transcription-options-toggle');
        if (!options || !toggle) return;
        const expanded = options.hidden;
        options.hidden = !expanded;
        toggle.setAttribute('aria-expanded', String(expanded));
        const icon = toggle.querySelector('.toggle-icon');
        if (icon) icon.classList.toggle('rotated', expanded);
    }

    /**
     * 切换令牌可见性
     */
    static toggleTokenVisibility() {
        const input = document.getElementById('bearer-token');
        const btn = document.getElementById('toggle-token-visibility');

        if (input.type === 'password') {
            input.type = 'text';
            btn.textContent = '🙈';
        } else {
            input.type = 'password';
            btn.textContent = '👁️';
        }
    }

    /**
     * 清除 Webhook 地址
     */
    static clearWebhook() {
        const input = document.getElementById('wechat-webhook');
        input.value = '';
        StorageManager.remove(APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK);

        // 显示提示
        UIManager.showStatus('success', 'Webhook 地址已清除', '已删除浏览器本地保存的 Webhook 地址');
        setTimeout(UIManager.hideStatus, 2000);
    }

    /**
     * 显示 Webhook 保存成功提示
     */
    static showWebhookSaved() {
        UIManager.showStatus('success', '✓ Webhook 地址已保存', '已自动保存到浏览器本地');
        setTimeout(UIManager.hideStatus, 1500);
    }
}

/**
 * 处理文本输入，实时URL提取和预览
 */
function handleTextInput(textarea) {
    const text = textarea.value;
    const urlResults = URLExtractor.extractAndRankURLs(text);

    const previewContainer = document.getElementById('url-preview');
    const inputFeedback = document.getElementById('input-feedback');
    previewContainer.hidden = urlResults.length === 0;

    if (urlResults.length === 0) {
        previewContainer.innerHTML = '';
        if (inputFeedback) {
            inputFeedback.textContent = text.trim() ? '未检测到可识别的视频链接，请检查分享文案。' : '';
            inputFeedback.hidden = !text.trim();
        }
        UIManager.updateSubmitButton();
        return;
    }
    
    // 显示提取的URL，最高分的作为默认选择
    let html = '<div class="detected-urls">';
    urlResults.forEach((result, index) => {
        const isDefault = index === 0;
        html += `
            <div class="url-option ${isDefault ? 'selected' : ''}" data-url="${escapeHTML(result.url)}">
                <input type="radio" name="selected-url" value="${escapeHTML(result.url)}" ${isDefault ? 'checked' : ''}>
                <label>
                    <span class="url-display">${escapeHTML(result.display)}</span>
                    <span class="url-score">评分: ${result.score}</span>
                </label>
            </div>
        `;
    });
    html += '</div>';
    
    previewContainer.innerHTML = html;
    if (inputFeedback) {
        inputFeedback.textContent = '';
        inputFeedback.hidden = true;
    }
    
    // 绑定选择事件
    bindURLSelection();
    UIManager.updateSubmitButton();
}

/**
 * 绑定URL选择事件
 */
function bindURLSelection() {
    const options = document.querySelectorAll('.url-option');
    
    options.forEach(option => {
        option.addEventListener('click', () => {
            // 移除所有选中状态
            options.forEach(opt => opt.classList.remove('selected'));
            
            // 添加选中状态
            option.classList.add('selected');
            
            // 选中对应的单选按钮
            const radio = option.querySelector('input[type="radio"]');
            radio.checked = true;
            
            UIManager.updateSubmitButton();
        });
    });
}

/**
 * 获取用户选择的URL
 */
function getSelectedURL() {
    const selected = document.querySelector('input[name="selected-url"]:checked');
    return selected ? selected.value : null;
}

/**
 * 复制文本到剪贴板
 */
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        UIManager.showStatus('success', '已复制到剪贴板', escapeHTML(text));
        setTimeout(UIManager.hideStatus, 2000);
    } catch (e) {
        console.error('复制失败:', e);
        UIManager.showStatus('error', '复制失败', '请手动复制链接');
        setTimeout(UIManager.hideStatus, 3000);
    }
}

/**
 * 提交转录任务
 */
async function submitTranscription(event) {
    event.preventDefault();
    
    if (currentTask) {
        return;
    }
    
    const selectedURL = getSelectedURL();
    const useSpeakerRecognition = document.getElementById('speaker-recognition').checked;
    const wechatWebhook = document.getElementById('wechat-webhook').value.trim();
    const originalText = document.getElementById('share-content').value.trim();

    if (!selectedURL) {
        const inputFeedback = document.getElementById('input-feedback');
        if (inputFeedback) {
            inputFeedback.textContent = '请先选择一个视频链接。';
            inputFeedback.hidden = false;
        }
        UIManager.showStatus('error', '请先选择一个视频链接', '请在上方文本框中输入包含视频链接的内容，系统会自动提取并显示可选的链接');
        setTimeout(UIManager.hideStatus, 5000);
        return;
    }

    const token = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
    if (!token) {
        const authPrompt = document.getElementById('auth-missing-prompt');
        if (authPrompt) authPrompt.hidden = false;
        UIManager.showStatus('error', '请先设置访问令牌', '点击“去设置”展开访问令牌输入框');
        setTimeout(UIManager.hideStatus, 5000);
        return;
    }

    try {
        currentTask = { url: selectedURL };
        UIManager.updateSubmitButton();
        UIManager.showStatus('loading', '正在提交转录任务...', '请稍候，正在处理您的请求');

        // 保存设置到本地存储
        StorageManager.set(APP_CONFIG.STORAGE_KEYS.SPEAKER_RECOGNITION, useSpeakerRecognition);

        const response = await APIManager.submitTranscription(selectedURL, useSpeakerRecognition, wechatWebhook);
        
        if (response.code === 202 && response.data && response.data.task_id) {
            const taskData = {
                task_id: response.data.task_id,
                view_token: response.data.view_token,
                url: selectedURL,
                original_text: originalText,
                use_speaker_recognition: useSpeakerRecognition
            };
            
            // 添加到历史记录
            const historyResult = TaskHistoryManager.addTask(taskData);

            // PWA E5 钩子（additive）：pwa.js 监听此事件做任务完成通知；
            // 无监听者时为零成本空操作
            document.dispatchEvent(new CustomEvent('vta:task-submitted', {
                detail: {
                    task_id: response.data.task_id,
                    view_token: response.data.view_token
                }
            }));
            
            // 根据是否重复显示不同的提示
            let statusMessage = '任务提交成功！';
            let statusDetails = `任务ID: ${response.data.task_id}<br>转录将在后台进行，完成后会通过配置的企业微信通知您<br>`;

            // PWA standalone 检测（T6）：独立窗口里 _blank 会逃逸到浏览器，
            // 结果页链接改同窗口打开；浏览器内行为不变
            const isStandalone = window.matchMedia('(display-mode: standalone)').matches
                || window.navigator.standalone === true;
            
            if (historyResult.isDuplicate) {
                statusMessage = '任务提交成功！(检测到重复URL)';
                statusDetails += `<span style="color: #f59e0b;">⚠️ 相同链接的旧任务记录已被更新</span><br>`;
            }
            
            statusDetails += `<a href="/view/${response.data.view_token}" target="${isStandalone ? '_self' : '_blank'}" style="color: #667eea; text-decoration: underline;">点击查看任务进度</a>`;
            
            UIManager.showStatus('success', statusMessage, statusDetails);
            
            // 清空表单
            document.getElementById('share-content').value = '';
            const previewContainer = document.getElementById('url-preview');
            previewContainer.innerHTML = '';
            previewContainer.hidden = true;
            const inputFeedback = document.getElementById('input-feedback');
            if (inputFeedback) {
                inputFeedback.textContent = '';
                inputFeedback.hidden = true;
            }
            
            // 3秒后跳转到查看页面
            // PWA standalone 模式下取消自动跳转（Codex R2-1）：/view 的
            // processing.html 是无 JS 的静态"请手动刷新"页，跳过去后 E5 轮询
            // 随页面离开死亡、完成通知静默失效。standalone 下用户停留在本页
            // 等通知（E5 简化版要求页面存活），结果页由上方 success 提示里的
            // 同窗口链接或通知 onclick 进入。浏览器内既有行为（3 秒后新标签页
            // 打开）不变。
            if (!isStandalone) {
                setTimeout(() => {
                    window.open(`/view/${response.data.view_token}`, '_blank');
                }, 3000);
            }
            
        } else {
            throw new Error(response.message || '提交失败');
        }
        
    } catch (error) {
        console.error('提交任务失败:', error);
        const inputFeedback = document.getElementById('input-feedback');
        if (inputFeedback) {
            inputFeedback.textContent = '提交失败，请检查链接和访问令牌后重试。';
            inputFeedback.hidden = false;
        }
        UIManager.showStatus('error', '提交任务失败', error.message);
    } finally {
        currentTask = null;
        UIManager.updateSubmitButton();
    }
}

/**
 * 格式化文件大小
 */
function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

/**
 * 构建上传元数据（恰六个字段）
 */
function buildUploadMetadata(file, options = {}) {
    const titleVal = options.title !== undefined ? options.title : (typeof document !== 'undefined' ? document.getElementById('upload-title')?.value : '');
    const sourceUrlVal = options.sourceUrl !== undefined ? options.sourceUrl : (typeof document !== 'undefined' ? document.getElementById('upload-source-url')?.value : '');
    const retentionVal = options.retention || getUploadRetention();

    const processingOptions = options.processingOptions || {
        calibrate: typeof document !== 'undefined' ? (document.getElementById('calibrate-option')?.checked ?? true) : true,
        summarize: typeof document !== 'undefined' ? (document.getElementById('summarize-option')?.checked ?? true) : true,
        infer_speaker_names: typeof document !== 'undefined' ? (document.getElementById('speaker-recognition')?.checked ?? false) : false,
        chapters: typeof document !== 'undefined' ? (document.getElementById('summarize-option')?.checked ?? true) : true,
    };

    const cleanTitle = (typeof titleVal === 'string' && titleVal.trim()) ? titleVal.trim().slice(0, 200) : null;
    const cleanSource = (typeof sourceUrlVal === 'string' && sourceUrlVal.trim()) ? sourceUrlVal.trim().slice(0, 2048) : null;

    return {
        filename: file.name,
        byte_size: file.size,
        title: cleanTitle,
        source_url: cleanSource,
        retention: retentionVal,
        processing_options: {
            calibrate: Boolean(processingOptions.calibrate),
            summarize: Boolean(processingOptions.summarize),
            infer_speaker_names: Boolean(processingOptions.infer_speaker_names),
            chapters: Boolean(processingOptions.chapters),
        }
    };
}

/**
 * base64url UTF-8 编码
 */
function encodeUploadMetadata(metadata) {
    const jsonStr = JSON.stringify(metadata);
    let binary = '';
    if (typeof TextEncoder !== 'undefined') {
        const bytes = new TextEncoder().encode(jsonStr);
        const len = bytes.byteLength;
        for (let i = 0; i < len; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
    } else {
        binary = unescape(encodeURIComponent(jsonStr));
    }
    return btoa(binary)
        .replace(/\+/g, '-')
        .replace(/\//g, '_')
        .replace(/=+$/, '');
}

/**
 * 生成 Idempotency-Key (epoch_ms-UUID)
 */
function generateUploadIdempotencyKey() {
    const epoch = Date.now();
    let uuid;
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        uuid = crypto.randomUUID();
    } else {
        uuid = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
            const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }
    return `${epoch}-${uuid}`;
}

/**
 * 获取选中的保留期限
 */
function getUploadRetention() {
    if (typeof document === 'undefined') return '30d';
    const radio = document.querySelector('input[name="upload-retention"]:checked');
    return radio ? radio.value : '30d';
}

/**
 * 选择音视频文件
 */
function selectUploadFile(file) {
    if (!file) return;
    selectedUploadFile = file;
    currentUploadIntent = null; // 新选文件重置意图

    // 每次新选文件重置保留期限为 30d
    const defaultRetention = (uploadCapabilities && uploadCapabilities.default_retention) || '30d';
    if (typeof document !== 'undefined') {
        const retentionRadios = document.querySelectorAll('input[name="upload-retention"]');
        retentionRadios.forEach(r => {
            r.checked = (r.value === defaultRetention);
        });

        const dropZonePrompt = document.getElementById('drop-zone-prompt');
        const fileInfo = document.getElementById('upload-file-info');
        const fileName = document.getElementById('upload-file-name');
        const fileSize = document.getElementById('upload-file-size');
        const feedback = document.getElementById('upload-input-feedback');

        if (dropZonePrompt) dropZonePrompt.hidden = true;
        if (fileInfo) fileInfo.hidden = false;
        if (fileName) fileName.textContent = file.name;
        if (fileSize) fileSize.textContent = formatBytes(file.size);

        let sizeError = null;
        if (uploadCapabilities && uploadCapabilities.limits && uploadCapabilities.limits.max_file_mib) {
            const maxBytes = uploadCapabilities.limits.max_file_mib * 1024 * 1024;
            if (file.size > maxBytes) {
                sizeError = `文件大小 (${formatBytes(file.size)}) 超出限制 (${uploadCapabilities.limits.max_file_mib} MiB)`;
            }
        }

        if (feedback) {
            if (sizeError) {
                feedback.textContent = sizeError;
                feedback.hidden = false;
            } else {
                feedback.textContent = '';
                feedback.hidden = true;
            }
        }
    }

    if (typeof UIManager !== 'undefined') {
        UIManager.updateSubmitButton();
    }
}

/**
 * 清除已选文件
 */
function clearUploadFile() {
    selectedUploadFile = null;
    currentUploadIntent = null;
    if (typeof document !== 'undefined') {
        const fileInput = document.getElementById('upload-file-input');
        if (fileInput) fileInput.value = '';

        const dropZonePrompt = document.getElementById('drop-zone-prompt');
        const fileInfo = document.getElementById('upload-file-info');
        const feedback = document.getElementById('upload-input-feedback');

        if (dropZonePrompt) dropZonePrompt.hidden = false;
        if (fileInfo) fileInfo.hidden = true;
        if (feedback) {
            feedback.textContent = '';
            feedback.hidden = true;
        }
    }

    if (typeof UIManager !== 'undefined') {
        UIManager.updateSubmitButton();
    }
}

/**
 * 切换转录模式 (url / upload)
 */
function switchMode(mode) {
    currentMode = mode;
    if (typeof document !== 'undefined') {
        const urlTab = document.getElementById('mode-tab-url');
        const uploadTab = document.getElementById('mode-tab-upload');
        const urlPanel = document.getElementById('url-mode-panel');
        const uploadPanel = document.getElementById('upload-mode-panel');

        if (mode === 'upload') {
            if (urlTab) {
                urlTab.classList.remove('active');
                urlTab.setAttribute('aria-selected', 'false');
            }
            if (uploadTab) {
                uploadTab.classList.add('active');
                uploadTab.setAttribute('aria-selected', 'true');
            }
            if (urlPanel) urlPanel.hidden = true;
            if (uploadPanel) uploadPanel.hidden = false;
            checkUploadCapabilities();
        } else {
            if (uploadTab) {
                uploadTab.classList.remove('active');
                uploadTab.setAttribute('aria-selected', 'false');
            }
            if (urlTab) {
                urlTab.classList.add('active');
                urlTab.setAttribute('aria-selected', 'true');
            }
            if (uploadPanel) uploadPanel.hidden = true;
            if (urlPanel) urlPanel.hidden = false;
        }
    }

    if (typeof UIManager !== 'undefined') {
        UIManager.updateSubmitButton();
    }
}

/**
 * 检查上传功能配置
 */
async function checkUploadCapabilities() {
    if (typeof document === 'undefined' || typeof fetch === 'undefined') return;
    if (typeof window !== 'undefined' && window.location && (!window.location.protocol || !window.location.protocol.startsWith('http'))) return;
    const authStorage = requireAuthStorage();
    const token = authStorage && authStorage.readAuthToken();
    if (!token) return;
    const banner = document.getElementById('upload-disabled-banner');
    const hint = document.getElementById('upload-limits-hint');
    try {
        const caps = await APIManager.getUploadCapabilities();
        if (typeof document === 'undefined') return;
        uploadCapabilities = caps;
        const banner = document.getElementById('upload-disabled-banner');
        const hint = document.getElementById('upload-limits-hint');
        if (caps && caps.enabled === false) {
            if (banner) banner.hidden = false;
        } else {
            if (banner) banner.hidden = true;
            if (hint && caps && caps.limits) {
                hint.textContent = `最大支持 ${caps.limits.max_file_mib} MiB，最多 ${caps.limits.max_media_hours} 小时`;
            }
        }
    } catch (err) {
        console.warn('获取上传功能配置失败:', err);
    }
    if (typeof document !== 'undefined' && typeof UIManager !== 'undefined') {
        UIManager.updateSubmitButton();
    }
}

/**
 * 设置拖拽区域
 */
function setupUploadDropZone() {
    if (typeof document === 'undefined') return;
    const dropZone = document.getElementById('upload-drop-zone');
    const fileInput = document.getElementById('upload-file-input');
    const clearBtn = document.getElementById('upload-file-clear');

    if (!dropZone || !fileInput) return;

    dropZone.addEventListener('click', (e) => {
        if (e.target === clearBtn || (clearBtn && clearBtn.contains(e.target))) {
            return;
        }
        fileInput.click();
    });

    dropZone.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            fileInput.click();
        }
    });

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('dragover');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            selectUploadFile(e.dataTransfer.files[0]);
        }
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files && e.target.files.length > 0) {
            selectUploadFile(e.target.files[0]);
        }
    });

    if (clearBtn) {
        clearBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            clearUploadFile();
        });
    }
}

/**
 * 提交文件上传表单
 */
async function submitUploadForm(event) {
    if (event && event.preventDefault) event.preventDefault();

    if (isUploading) {
        return;
    }

    if (!selectedUploadFile) {
        const feedback = document.getElementById('upload-input-feedback');
        if (feedback) {
            feedback.textContent = '请先选择一个音视频文件。';
            feedback.hidden = false;
        }
        UIManager.showStatus('error', '请先选择文件', '点击或拖拽选择本地音视频文件后重试');
        setTimeout(UIManager.hideStatus, 5000);
        return;
    }

    if (uploadCapabilities && uploadCapabilities.enabled === false) {
        UIManager.showStatus('error', '上传服务未开启', '当前服务端已关闭本地文件上传能力');
        return;
    }

    if (uploadCapabilities && uploadCapabilities.limits && uploadCapabilities.limits.max_file_mib) {
        if (selectedUploadFile.size > uploadCapabilities.limits.max_file_mib * 1024 * 1024) {
            UIManager.showStatus('error', '文件超出限制', `文件大小超出上限 ${uploadCapabilities.limits.max_file_mib} MiB`);
            return;
        }
    }

    const token = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
    if (!token) {
        const authPrompt = document.getElementById('auth-missing-prompt');
        if (authPrompt) authPrompt.hidden = false;
        UIManager.showStatus('error', '请先设置访问令牌', '点击“去设置”展开访问令牌输入框');
        setTimeout(UIManager.hideStatus, 5000);
        return;
    }

    if (!currentUploadIntent || currentUploadIntent.file !== selectedUploadFile) {
        currentUploadIntent = {
            file: selectedUploadFile,
            idempotencyKey: generateUploadIdempotencyKey()
        };
    }

    const { idempotencyKey } = currentUploadIntent;
    const metadata = buildUploadMetadata(selectedUploadFile);

    isUploading = true;
    UIManager.updateSubmitButton();
    UIManager.showStatus('loading', '正在上传音视频文件...', '请稍候，文件正在传输并提交受理');

    let receipt = null;
    try {
        receipt = await APIManager.submitUpload(selectedUploadFile, idempotencyKey, metadata);
    } catch (err) {
        if (err.status) {
            isUploading = false;
            UIManager.updateSubmitButton();
            UIManager.showStatus('error', '上传失败', err.message || `HTTP ${err.status}`);
            return;
        }

        // 网络未知异常：走 receipt-only 查询，不重发 body
        UIManager.showStatus('loading', '网络连接异常，正在核实受理状态...', '未重复发送文件，正在查询回执');
        try {
            receipt = await APIManager.getUploadReceipt(idempotencyKey);
        } catch (receiptErr) {
            isUploading = false;
            UIManager.updateSubmitButton();
            UIManager.showStatus('error', '上传状态未知且查询失败', `网络异常，且未能通过回执核实状态: ${receiptErr.message}`);
            return;
        }
    } finally {
        isUploading = false;
        UIManager.updateSubmitButton();
    }

    if (receipt) {
        const taskData = {
            task_id: receipt.task_id,
            view_token: receipt.view_token,
            upload_id: receipt.upload_id,
            source: 'upload',
            title: metadata.title || selectedUploadFile.name,
            original_text: metadata.source_url || '',
            retention: receipt.retention,
            share_active: receipt.share_active
        };
        TaskHistoryManager.addTask(taskData);

        const isStandalone = (typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
            || (typeof window !== 'undefined' && window.navigator && window.navigator.standalone === true);

        let statusMessage = '文件上传成功，任务已受理！';
        let statusDetails = `上传ID: ${escapeHTML(receipt.upload_id)}<br>` +
            (receipt.task_id ? `任务ID: ${escapeHTML(receipt.task_id)}<br>` : '') +
            `状态: ${escapeHTML(receipt.state)}<br>` +
            (receipt.expires_at ? `保留至: ${escapeHTML(receipt.expires_at)}<br>` : '') +
            (receipt.view_token ? `<a href="/view/${escapeHTML(receipt.view_token)}" target="${isStandalone ? '_self' : '_blank'}" style="color: #667eea; text-decoration: underline;">点击查看转录结果与进度</a>` : '');

        UIManager.showStatus('success', statusMessage, statusDetails);

        if (!isStandalone && receipt.view_token) {
            setTimeout(() => {
                if (typeof window !== 'undefined' && window.open) {
                    window.open(`/view/${receipt.view_token}`, '_blank');
                }
            }, 3000);
        }

        clearUploadFile();
    }
}

/**
 * 页面初始化
 */
function initializePage() {
    console.log('初始化视频转录Web应用...');

    if (!getAuthStorage()) {
        disableProtectedActions();
    }

    // 加载保存的设置
    const savedToken = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
    const savedWebhook = StorageManager.get(APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK);
    const savedSpeakerRecognition = StorageManager.get(APP_CONFIG.STORAGE_KEYS.SPEAKER_RECOGNITION);

    if (savedToken) {
        document.getElementById('bearer-token').value = savedToken;
    }

    // 加载 webhook 地址
    if (savedWebhook) {
        document.getElementById('wechat-webhook').value = savedWebhook;
    }

    if (savedSpeakerRecognition !== null) {
        document.getElementById('speaker-recognition').checked = savedSpeakerRecognition;
    }
    
    // 绑定事件监听器
    const textarea = document.getElementById('share-content');
    textarea.value = ''; // 确保初始为空
    textarea.addEventListener('input', () => handleTextInput(textarea));
    
    // 空输入时不占用首屏空间
    const previewContainer = document.getElementById('url-preview');
    previewContainer.innerHTML = '';
    previewContainer.hidden = true;
    
    const form = document.getElementById('transcribe-form');
    form.addEventListener('submit', (e) => {
        if (currentMode === 'upload') {
            submitUploadForm(e);
        } else {
            submitTranscription(e);
        }
    });

    // 模式切换
    const urlTab = document.getElementById('mode-tab-url');
    const uploadTab = document.getElementById('mode-tab-upload');
    if (urlTab) urlTab.addEventListener('click', () => switchMode('url'));
    if (uploadTab) uploadTab.addEventListener('click', () => switchMode('upload'));

    // 设置上传拖拽与点击选择
    setupUploadDropZone();

    // 预检上传功能开关
    checkUploadCapabilities();
    
    const advancedToggle = document.getElementById('advanced-toggle');
    advancedToggle.addEventListener('click', UIManager.toggleAdvancedSettings);

    const transcriptionOptionsToggle = document.getElementById('transcription-options-toggle');
    transcriptionOptionsToggle.addEventListener('click', UIManager.toggleTranscriptionOptions);

    const authSettingsLink = document.getElementById('auth-settings-link');
    authSettingsLink.addEventListener('click', () => {
        if (!isAdvancedSettingsExpanded) UIManager.toggleAdvancedSettings();
        const tokenInput = document.getElementById('bearer-token');
        if (tokenInput) tokenInput.focus();
    });

    const tokenToggle = document.getElementById('toggle-token-visibility');
    tokenToggle.addEventListener('click', UIManager.toggleTokenVisibility);

    const clearWebhookBtn = document.getElementById('clear-webhook');
    clearWebhookBtn.addEventListener('click', UIManager.clearWebhook);

    // 监听设置变化
    document.getElementById('bearer-token').addEventListener('input', (e) => {
        const saved = StorageManager.set(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN, e.target.value);
        if (saved === false) {
            e.target.value = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN) || '';
            UIManager.showStatus('error', '访问令牌保存失败', '仍使用当前访问令牌');
        }
        checkUploadCapabilities();
        UIManager.updateSubmitButton();
    });

    window.addEventListener('storage', handleHomepageAuthStorageEvent);

    document.getElementById('wechat-webhook').addEventListener('input', (e) => {
        const webhookValue = e.target.value;

        // 立即保存到 localStorage
        StorageManager.set(APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK, webhookValue);

        // 清除之前的定时器
        if (webhookSaveTimer) {
            clearTimeout(webhookSaveTimer);
        }

        // 设置新的定时器：用户停止输入 1 秒后显示保存成功提示
        if (webhookValue.trim() !== '') {
            webhookSaveTimer = setTimeout(() => {
                UIManager.showWebhookSaved();
            }, 1000);
        }
    });

    document.getElementById('speaker-recognition').addEventListener('change', (e) => {
        StorageManager.set(APP_CONFIG.STORAGE_KEYS.SPEAKER_RECOGNITION, e.target.checked);
        const summary = document.getElementById('transcription-options-summary');
        if (summary) summary.textContent = e.target.checked ? '已启用说话人识别' : '未启用说话人识别';
    });

    const speakerSummary = document.getElementById('transcription-options-summary');
    if (speakerSummary && savedSpeakerRecognition) {
        speakerSummary.textContent = '已启用说话人识别';
    }
    
    // 渲染任务历史
    TaskHistoryManager.renderHistory();
    
    // 初始化主题系统
    ThemeManager.initialize();
    
    // 初始状态更新
    UIManager.updateSubmitButton();
    
    console.log('视频转录Web应用初始化完成');
}

// 页面加载完成后初始化（vitest 无 DOM 环境跳过接线）
if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', initializePage);
}

// 导出全局函数供HTML使用
if (typeof window !== 'undefined') {
    window.copyToClipboard = copyToClipboard;
    window.buildUploadMetadata = buildUploadMetadata;
    window.encodeUploadMetadata = encodeUploadMetadata;
    window.generateUploadIdempotencyKey = generateUploadIdempotencyKey;
    window.selectUploadFile = selectUploadFile;
    window.clearUploadFile = clearUploadFile;
    window.getUploadRetention = getUploadRetention;
    window.switchMode = switchMode;
    window.checkUploadCapabilities = checkUploadCapabilities;
    window.submitUploadForm = submitUploadForm;
    window.APIManager = APIManager;
    window.UIManager = UIManager;
    window.TaskHistoryManager = TaskHistoryManager;
}

// 导出纯函数供 vitest（CJS，经 createRequire 加载）
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        escapeHTML,
        buildHistoryItemHTML,
        StorageManager,
        APIManager,
        UIManager,
        TaskHistoryManager,
        getAuthStorage,
        disableProtectedActions,
        AUTH_STORAGE_ERROR_MESSAGE,
        buildUploadMetadata,
        encodeUploadMetadata,
        generateUploadIdempotencyKey,
        selectUploadFile,
        clearUploadFile,
        getUploadRetention,
        switchMode,
        checkUploadCapabilities,
        submitUploadForm,
    };
}
