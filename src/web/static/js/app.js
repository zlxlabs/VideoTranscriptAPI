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
    MAX_HISTORY_ITEMS: 10,
    ENCRYPTION_KEY: 'vta_encrypt_key_2024' // 简单的加密密钥
};

// 全局变量
let currentTask = null;
let isAdvancedSettingsExpanded = false;

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
 * 简单加密函数（Base64 + 简单混淆）
 */
function simpleEncrypt(text) {
    if (!text) return '';
    try {
        const encoded = btoa(unescape(encodeURIComponent(text + APP_CONFIG.ENCRYPTION_KEY)));
        return encoded.split('').reverse().join('');
    } catch (e) {
        console.error('加密失败:', e);
        return text;
    }
}

/**
 * 简单解密函数
 */
function simpleDecrypt(encoded) {
    if (!encoded) return '';
    try {
        const reversed = encoded.split('').reverse().join('');
        const decoded = decodeURIComponent(escape(atob(reversed)));
        return decoded.replace(APP_CONFIG.ENCRYPTION_KEY, '');
    } catch (e) {
        console.error('解密失败:', e);
        return encoded;
    }
}

/**
 * 本地存储管理类
 */
class StorageManager {
    static set(key, value) {
        try {
            if (key === APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN || key === APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK) {
                // 敏感信息加密存储
                localStorage.setItem(key, simpleEncrypt(value));
            } else {
                localStorage.setItem(key, JSON.stringify(value));
            }
        } catch (e) {
            console.error('存储失败:', e);
        }
    }

    static get(key) {
        try {
            const value = localStorage.getItem(key);
            if (!value) return null;

            if (key === APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN || key === APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK) {
                // 敏感信息解密
                return simpleDecrypt(value);
            } else {
                return JSON.parse(value);
            }
        } catch (e) {
            console.error('读取存储失败:', e);
            return null;
        }
    }

    static remove(key) {
        try {
            localStorage.removeItem(key);
        } catch (e) {
            console.error('删除存储失败:', e);
        }
    }

    static clear() {
        try {
            Object.values(APP_CONFIG.STORAGE_KEYS).forEach(key => {
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
            'xiaohongshu.com', 'xhslink.com', 'douyin.com', 'v.douyin.com',
            'xiaoyuzhoufm.com', 'tiktok.com', 'vm.tiktok.com'
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
    static async submitTranscription(url, useSpeakerRecognition, webhook) {
        const token = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
        
        if (!token) {
            throw new Error('请先设置API访问令牌');
        }

        const response = await fetch('/api/transcribe', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({
                url: url,
                use_speaker_recognition: useSpeakerRecognition,
                wechat_webhook: webhook || undefined
            })
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
        const token = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
        
        if (!token) {
            throw new Error('请先设置API访问令牌');
        }

        const response = await fetch(`/api/task/${taskId}`, {
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ message: '查询失败' }));
            throw new Error(errorData.message || `HTTP ${response.status}`);
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
                url: taskData.url,
                original_text: taskData.original_text || '',
                title: taskData.title || this.extractTitleFromURL(taskData.url),
                timestamp: Date.now(),
                useSpeakerRecognition: taskData.use_speaker_recognition || false,
                status: 'submitted'
            };

            // 基于URL去重：相同URL只保留最新的记录
            const existingUrlIndex = history.findIndex(task => task.url === newTask.url);
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
            } else if (hostname.includes('xiaohongshu.com')) {
                return '小红书内容';
            } else if (hostname.includes('douyin.com')) {
                return '抖音视频';
            } else if (hostname.includes('xiaoyuzhoufm.com')) {
                return '小宇宙播客';
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
            
            const timeStr = new Date(task.timestamp).toLocaleString('zh-CN');
            const originalTextPreview = task.original_text ? 
                (task.original_text.length > 100 ? task.original_text.substring(0, 100) + '...' : task.original_text) : '';
            
            item.innerHTML = `
                <div class="history-info">
                    <div class="history-title">${task.title}</div>
                    ${originalTextPreview ? `
                        <div class="history-original-text">
                            <span class="original-text-label">原始内容：</span>
                            <span class="original-text-content">${originalTextPreview}</span>
                        </div>
                    ` : ''}
                    <div class="history-url">${task.url}</div>
                    <div class="history-meta">
                        <span>${timeStr}</span>
                        ${task.useSpeakerRecognition ? '<span class="feature-tag">• 说话人识别</span>' : ''}
                    </div>
                </div>
                <div class="history-actions">
                    <button class="history-btn" onclick="copyToClipboard('${task.url}')">📋 复制</button>
                    <a class="history-btn" href="/view/${task.view_token || task.id}" target="_blank">👁️ 查看</a>
                    <button class="history-btn delete-btn" onclick="TaskHistoryManager.deleteTask('${task.id}')">🗑️ 删除</button>
                </div>
            `;
            
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
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
            return 'dark';
        }
        return 'light';
    }
    
    /**
     * 应用主题
     */
    static applyTheme(theme) {
        const body = document.body;
        const themeToggle = document.getElementById('theme-toggle');
        
        if (theme === 'dark') {
            body.setAttribute('data-theme', 'dark');
            if (themeToggle) {
                themeToggle.textContent = '☀️';
                themeToggle.title = '切换到浅色模式';
            }
        } else {
            body.removeAttribute('data-theme');
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
        return document.body.hasAttribute('data-theme') ? 'dark' : 'light';
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
        const btn = document.getElementById('submit-btn');
        const btnIcon = btn.querySelector('.btn-icon');
        const btnText = btn.querySelector('.btn-text');
        
        const selectedURL = getSelectedURL();
        const token = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
        const webhook = document.getElementById('wechat-webhook').value.trim();
        
        const canSubmit = selectedURL && token && webhook && !currentTask;
        
        btn.disabled = !canSubmit;
        
        if (currentTask) {
            btnIcon.textContent = '⏳';
            btnText.textContent = '处理中...';
        } else if (!selectedURL) {
            btnIcon.textContent = '🚀';
            btnText.textContent = '请先输入视频链接';
        } else if (!webhook) {
            btnIcon.textContent = '📱';
            btnText.textContent = '请先设置企业微信通知地址';
        } else if (!token) {
            btnIcon.textContent = '🔐';
            btnText.textContent = '请先设置访问令牌';
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
        const icon = document.querySelector('.toggle-icon');
        
        isAdvancedSettingsExpanded = !isAdvancedSettingsExpanded;
        
        if (isAdvancedSettingsExpanded) {
            settings.classList.add('expanded');
            icon.classList.add('rotated');
        } else {
            settings.classList.remove('expanded');
            icon.classList.remove('rotated');
        }
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
}

/**
 * 处理文本输入，实时URL提取和预览
 */
function handleTextInput(textarea) {
    const text = textarea.value;
    const urlResults = URLExtractor.extractAndRankURLs(text);
    
    const previewContainer = document.getElementById('url-preview');
    
    if (urlResults.length === 0) {
        previewContainer.innerHTML = '<div class="no-urls">未检测到URL</div>';
        UIManager.updateSubmitButton();
        return;
    }
    
    // 显示提取的URL，最高分的作为默认选择
    let html = '<div class="detected-urls">';
    urlResults.forEach((result, index) => {
        const isDefault = index === 0;
        html += `
            <div class="url-option ${isDefault ? 'selected' : ''}" data-url="${result.url}">
                <input type="radio" name="selected-url" value="${result.url}" ${isDefault ? 'checked' : ''}>
                <label>
                    <span class="url-display">${result.display}</span>
                    <span class="url-score">评分: ${result.score}</span>
                </label>
            </div>
        `;
    });
    html += '</div>';
    
    previewContainer.innerHTML = html;
    
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
        UIManager.showStatus('success', '已复制到剪贴板', text);
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
    const webhook = document.getElementById('wechat-webhook').value.trim();
    const originalText = document.getElementById('share-content').value.trim();
    
    if (!selectedURL) {
        UIManager.showStatus('error', '请先选择一个视频链接');
        return;
    }
    
    if (!webhook) {
        UIManager.showStatus('error', '请先设置企业微信通知地址', '企业微信Webhook地址为必填项，用于接收转录进度通知');
        return;
    }
    
    // 验证Webhook URL格式
    try {
        new URL(webhook);
    } catch (e) {
        UIManager.showStatus('error', '企业微信Webhook地址格式无效', '请输入正确的Webhook URL格式');
        return;
    }
    
    try {
        currentTask = { url: selectedURL };
        UIManager.updateSubmitButton();
        UIManager.showStatus('loading', '正在提交转录任务...', '请稍候，正在处理您的请求');
        
        // 保存设置到本地存储
        StorageManager.set(APP_CONFIG.STORAGE_KEYS.SPEAKER_RECOGNITION, useSpeakerRecognition);
        if (webhook) {
            StorageManager.set(APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK, webhook);
        }
        
        const response = await APIManager.submitTranscription(selectedURL, useSpeakerRecognition, webhook);
        
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
            
            // 根据是否重复显示不同的提示
            let statusMessage = '任务提交成功！';
            let statusDetails = `任务ID: ${response.data.task_id}<br>转录将在后台进行，完成后会通过企业微信通知您<br>`;
            
            if (historyResult.isDuplicate) {
                statusMessage = '任务提交成功！(检测到重复URL)';
                statusDetails += `<span style="color: #f59e0b;">⚠️ 相同链接的旧任务记录已被更新</span><br>`;
            }
            
            statusDetails += `<a href="/view/${response.data.view_token}" target="_blank" style="color: #667eea; text-decoration: underline;">点击查看任务进度</a>`;
            
            UIManager.showStatus('success', statusMessage, statusDetails);
            
            // 清空表单
            document.getElementById('share-content').value = '';
            document.getElementById('url-preview').innerHTML = '<div class="no-urls">请输入包含视频链接的内容</div>';
            
            // 3秒后跳转到查看页面
            setTimeout(() => {
                window.open(`/view/${response.data.view_token}`, '_blank');
            }, 3000);
            
        } else {
            throw new Error(response.message || '提交失败');
        }
        
    } catch (error) {
        console.error('提交任务失败:', error);
        UIManager.showStatus('error', '提交任务失败', error.message);
    } finally {
        currentTask = null;
        UIManager.updateSubmitButton();
    }
}

/**
 * 页面初始化
 */
function initializePage() {
    console.log('初始化视频转录Web应用...');
    
    // 加载保存的设置
    const savedToken = StorageManager.get(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN);
    const savedWebhook = StorageManager.get(APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK);
    const savedSpeakerRecognition = StorageManager.get(APP_CONFIG.STORAGE_KEYS.SPEAKER_RECOGNITION);
    
    if (savedToken) {
        document.getElementById('bearer-token').value = savedToken;
    }
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
    
    // 确保URL预览区域初始状态正确
    const previewContainer = document.getElementById('url-preview');
    previewContainer.innerHTML = '<div class="no-urls">请输入包含视频链接的内容</div>';
    
    const form = document.getElementById('transcribe-form');
    form.addEventListener('submit', submitTranscription);
    
    const advancedToggle = document.getElementById('advanced-toggle');
    advancedToggle.addEventListener('click', UIManager.toggleAdvancedSettings);
    
    const tokenToggle = document.getElementById('toggle-token-visibility');
    tokenToggle.addEventListener('click', UIManager.toggleTokenVisibility);
    
    const clearWebhook = document.getElementById('clear-webhook');
    clearWebhook.addEventListener('click', () => {
        document.getElementById('wechat-webhook').value = '';
        StorageManager.remove(APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK);
    });
    
    // 监听设置变化
    document.getElementById('bearer-token').addEventListener('input', (e) => {
        StorageManager.set(APP_CONFIG.STORAGE_KEYS.BEARER_TOKEN, e.target.value);
        UIManager.updateSubmitButton();
    });
    
    document.getElementById('wechat-webhook').addEventListener('input', (e) => {
        if (e.target.value.trim()) {
            StorageManager.set(APP_CONFIG.STORAGE_KEYS.WECHAT_WEBHOOK, e.target.value.trim());
        }
        UIManager.updateSubmitButton();
    });
    
    document.getElementById('speaker-recognition').addEventListener('change', (e) => {
        StorageManager.set(APP_CONFIG.STORAGE_KEYS.SPEAKER_RECOGNITION, e.target.checked);
    });
    
    // 渲染任务历史
    TaskHistoryManager.renderHistory();
    
    // 初始化主题系统
    ThemeManager.initialize();
    
    // 初始状态更新
    UIManager.updateSubmitButton();
    
    console.log('视频转录Web应用初始化完成');
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', initializePage);

// 导出全局函数供HTML使用
window.copyToClipboard = copyToClipboard;