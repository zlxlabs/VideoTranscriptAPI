/**
 * 浮动 TOC (Table of Contents) 功能
 * 支持 PC 端和移动端响应式设计
 *
 * 功能特性：
 * - 自动提取内容总结区块的 H1-H4 标题
 * - PC 端：右侧浮动，悬停展开，支持 Pin 固定
 * - 移动端：底部浮动按钮，点击弹出半屏面板
 * - 滚动自动高亮当前标题
 * - 点击平滑跳转
 * - 主题自适应
 */

(function() {
    'use strict';

    // ========== 配置常量 ==========
    const CONFIG = {
        // 本地存储键名
        STORAGE_KEY: 'vta_toc_pinned',

        // 标题选择器：仅从"内容总结"区块提取 H1-H4
        HEADING_SELECTOR: '.section:has(h2:contains("内容总结")) .content h1, ' +
                         '.section:has(h2:contains("内容总结")) .content h2, ' +
                         '.section:has(h2:contains("内容总结")) .content h3, ' +
                         '.section:has(h2:contains("内容总结")) .content h4',

        // 校对文本区块选择器
        CALIBRATED_SELECTOR: '.section:has(h2:contains("校对文本"))',

        // IntersectionObserver 配置
        OBSERVER_OPTIONS: {
            threshold: 0.5,
            rootMargin: '-100px 0px -60% 0px'
        },

        // 移动端断点
        MOBILE_BREAKPOINT: 768
    };

    // ========== 全局变量 ==========
    let tocData = {
        headings: [],
        calibratedSection: null
    };

    let observer = null;
    let isPinned = false;
    let isMobile = false;

    // ========== 工具函数 ==========

    /**
     * 检查是否为移动设备
     */
    function checkMobile() {
        return window.innerWidth <= CONFIG.MOBILE_BREAKPOINT;
    }

    /**
     * 生成唯一 ID
     */
    function generateId(text, index) {
        const slug = text
            .toLowerCase()
            .replace(/[^\w\s\u4e00-\u9fa5-]/g, '')
            .replace(/\s+/g, '-')
            .substring(0, 50);
        return `toc-heading-${slug}-${index}`;
    }

    /**
     * 获取 Pin 状态
     */
    function loadPinState() {
        try {
            const state = localStorage.getItem(CONFIG.STORAGE_KEY);
            return state === 'true';
        } catch (e) {
            console.warn('Failed to load TOC pin state:', e);
            return false;
        }
    }

    /**
     * 保存 Pin 状态
     */
    function savePinState(pinned) {
        try {
            localStorage.setItem(CONFIG.STORAGE_KEY, pinned.toString());
        } catch (e) {
            console.warn('Failed to save TOC pin state:', e);
        }
    }

    // ========== 数据提取 ==========

    /**
     * 提取页面标题数据
     */
    function extractHeadings() {
        const headings = [];

        // 由于 :contains 不是标准选择器，我们需要手动查找
        const summarySection = Array.from(document.querySelectorAll('.section')).find(section => {
            const h2 = section.querySelector('h2');
            return h2 && h2.textContent.includes('内容总结');
        });

        if (!summarySection) {
            console.warn('未找到"内容总结"区块');
            return headings;
        }

        const contentDiv = summarySection.querySelector('.content');
        if (!contentDiv) {
            console.warn('未找到内容区域');
            return headings;
        }

        // 提取 H1-H4 标题
        const headingElements = contentDiv.querySelectorAll('h1, h2, h3, h4');

        headingElements.forEach((element, index) => {
            const level = parseInt(element.tagName.substring(1));
            const text = element.textContent.trim();

            if (!text) return;

            // 确保标题有 ID
            if (!element.id) {
                element.id = generateId(text, index);
            }

            headings.push({
                level: level,
                text: text,
                id: element.id,
                element: element
            });
        });

        console.log(`提取到 ${headings.length} 个标题`);
        return headings;
    }

    /**
     * 查找校对文本区块
     */
    function findCalibratedSection() {
        const sections = Array.from(document.querySelectorAll('.section'));
        return sections.find(section => {
            const h2 = section.querySelector('h2');
            return h2 && h2.textContent.includes('校对文本');
        });
    }

    // ========== UI 渲染 ==========

    /**
     * 创建 PC 端 TOC 结构
     */
    function createPCTocHTML() {
        const headings = tocData.headings;
        const hasCalibratedSection = !!tocData.calibratedSection;

        let headingsHTML = '';

        if (headings.length > 0) {
            headingsHTML += '<div class="toc-section-title">📝 内容总结</div>';
            headings.forEach(heading => {
                const levelClass = `data-level="${heading.level}"`;
                headingsHTML += `
                    <div class="toc-item">
                        <a class="toc-link" href="#${heading.id}" ${levelClass} data-id="${heading.id}">
                            ${heading.text}
                        </a>
                    </div>
                `;
            });
        }

        if (hasCalibratedSection) {
            headingsHTML += `
                <div class="toc-item">
                    <a class="toc-link toc-anchor" href="#calibrated-section" data-id="calibrated-section">
                        ✨ 校对文本
                    </a>
                </div>
            `;
        }

        return `
            <div class="floating-toc-container collapsed" id="floating-toc">
                <div class="toc-indicator">
                    <div class="toc-indicator-line"></div>
                    <div class="toc-indicator-line"></div>
                    <div class="toc-indicator-line"></div>
                    <div class="toc-indicator-line"></div>
                </div>
                <div class="toc-header">
                    <div class="toc-title">📑 目录</div>
                    <button class="toc-pin-btn" id="toc-pin-btn" title="固定目录（点击保持展开）">📌</button>
                </div>
                <div class="toc-content">
                    <ul class="toc-list">
                        ${headingsHTML}
                    </ul>
                </div>
            </div>
        `;
    }

    /**
     * 创建移动端 TOC 结构
     */
    function createMobileTocHTML() {
        const headings = tocData.headings;
        const hasCalibratedSection = !!tocData.calibratedSection;

        let headingsHTML = '';

        if (headings.length > 0) {
            headings.forEach(heading => {
                const levelClass = `data-level="${heading.level}"`;
                headingsHTML += `
                    <div class="toc-item">
                        <a class="toc-link" href="#${heading.id}" ${levelClass} data-id="${heading.id}">
                            ${heading.text}
                        </a>
                    </div>
                `;
            });
        }

        if (hasCalibratedSection) {
            headingsHTML += `
                <div class="toc-item">
                    <a class="toc-link toc-anchor" href="#calibrated-section" data-id="calibrated-section">
                        ✨ 校对文本
                    </a>
                </div>
            `;
        }

        return `
            <button class="floating-toc-mobile-btn" id="toc-mobile-btn" title="目录">
                📑
            </button>
            <div class="floating-toc-mobile-panel" id="toc-mobile-panel">
                <div class="toc-mobile-overlay" id="toc-mobile-overlay"></div>
                <div class="toc-mobile-content">
                    <div class="toc-mobile-header">
                        <div class="toc-mobile-title">📑 目录</div>
                        <button class="toc-mobile-close-btn" id="toc-mobile-close-btn">✕</button>
                    </div>
                    <div class="toc-mobile-body">
                        <ul class="toc-list">
                            ${headingsHTML}
                        </ul>
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * 渲染 TOC 到页面
     */
    function renderTOC() {
        // 移除已存在的 TOC
        const existingPC = document.getElementById('floating-toc');
        const existingMobileBtn = document.getElementById('toc-mobile-btn');
        const existingMobilePanel = document.getElementById('toc-mobile-panel');

        if (existingPC) existingPC.remove();
        if (existingMobileBtn) existingMobileBtn.remove();
        if (existingMobilePanel) existingMobilePanel.remove();

        // 如果没有标题，不渲染
        if (tocData.headings.length === 0 && !tocData.calibratedSection) {
            console.log('没有标题数据，跳过 TOC 渲染');
            return;
        }

        // 创建并插入 PC 端 TOC
        const pcTocHTML = createPCTocHTML();
        document.body.insertAdjacentHTML('beforeend', pcTocHTML);

        // 创建并插入移动端 TOC
        const mobileTocHTML = createMobileTocHTML();
        document.body.insertAdjacentHTML('beforeend', mobileTocHTML);

        console.log('TOC 渲染完成');
    }

    // ========== 事件处理 ==========

    /**
     * 处理 TOC 链接点击
     */
    function handleTocClick(e) {
        const link = e.target.closest('.toc-link');
        if (!link) return;

        e.preventDefault();

        const targetId = link.dataset.id;
        let targetElement = null;

        // 查找目标元素
        if (targetId === 'calibrated-section') {
            targetElement = tocData.calibratedSection;
        } else {
            targetElement = document.getElementById(targetId);
        }

        if (!targetElement) {
            console.warn('未找到目标元素:', targetId);
            return;
        }

        // 平滑滚动
        targetElement.scrollIntoView({
            behavior: 'smooth',
            block: 'start'
        });

        // 移动端：关闭面板
        if (isMobile) {
            closeMobilePanel();
        }

        // 更新激活状态
        setTimeout(() => {
            updateActiveLink(targetId);
        }, 100);
    }

    /**
     * 处理 Pin 按钮点击
     */
    function handlePinClick() {
        const container = document.getElementById('floating-toc');
        const pinBtn = document.getElementById('toc-pin-btn');

        if (!container || !pinBtn) return;

        isPinned = !isPinned;

        // 添加点击动画
        if (isPinned) {
            // 固定动画
            pinBtn.classList.add('animating-pin');
            setTimeout(() => {
                pinBtn.classList.remove('animating-pin');
            }, 400);

            container.classList.add('pinned');
            container.classList.remove('collapsed');
            pinBtn.classList.add('pinned');
            pinBtn.title = '取消固定目录（已固定）';
        } else {
            // 取消固定动画
            pinBtn.classList.add('animating-unpin');
            setTimeout(() => {
                pinBtn.classList.remove('animating-unpin');
            }, 400);

            container.classList.remove('pinned');
            container.classList.add('collapsed');
            pinBtn.classList.remove('pinned');
            pinBtn.title = '固定目录（点击保持展开）';
        }

        savePinState(isPinned);
    }

    /**
     * 打开移动端面板
     */
    function openMobilePanel() {
        const panel = document.getElementById('toc-mobile-panel');
        if (panel) {
            panel.classList.add('show');
            document.body.style.overflow = 'hidden';
        }
    }

    /**
     * 关闭移动端面板
     */
    function closeMobilePanel() {
        const panel = document.getElementById('toc-mobile-panel');
        if (panel) {
            panel.classList.remove('show');
            document.body.style.overflow = '';
        }
    }

    /**
     * 更新激活的链接
     */
    function updateActiveLink(activeId) {
        const links = document.querySelectorAll('.toc-link');
        links.forEach(link => {
            if (link.dataset.id === activeId) {
                link.classList.add('active');
            } else {
                link.classList.remove('active');
            }
        });
    }

    // ========== 滚动监听 ==========

    /**
     * 设置 IntersectionObserver
     */
    function setupScrollObserver() {
        // 清理旧的 observer
        if (observer) {
            observer.disconnect();
        }

        // 获取所有需要观察的元素
        const elements = tocData.headings.map(h => h.element);
        if (tocData.calibratedSection) {
            elements.push(tocData.calibratedSection);
        }

        if (elements.length === 0) return;

        // 创建 observer
        observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const id = entry.target.id || 'calibrated-section';
                    updateActiveLink(id);
                }
            });
        }, CONFIG.OBSERVER_OPTIONS);

        // 观察所有元素
        elements.forEach(element => {
            if (element) observer.observe(element);
        });

        console.log('滚动监听已设置');
    }

    // ========== 响应式处理 ==========

    /**
     * 处理窗口大小变化
     */
    function handleResize() {
        const wasMobile = isMobile;
        isMobile = checkMobile();

        // 移动端切换时，关闭移动端面板
        if (wasMobile && !isMobile) {
            closeMobilePanel();
        }
    }

    // ========== 初始化 ==========

    /**
     * 绑定事件监听器
     */
    function bindEvents() {
        // 使用事件委托绑定所有点击事件
        document.addEventListener('click', (e) => {
            // PC 端 Pin 按钮
            if (e.target.closest('#toc-pin-btn')) {
                handlePinClick();
                return;
            }

            // 移动端浮动按钮
            if (e.target.closest('#toc-mobile-btn')) {
                openMobilePanel();
                return;
            }

            // 移动端关闭按钮
            if (e.target.closest('#toc-mobile-close-btn')) {
                e.preventDefault();
                e.stopPropagation();
                closeMobilePanel();
                return;
            }

            // 移动端遮罩层
            if (e.target.closest('#toc-mobile-overlay')) {
                closeMobilePanel();
                return;
            }

            // TOC 链接点击
            if (e.target.closest('.toc-link')) {
                handleTocClick(e);
                return;
            }
        });

        // 窗口大小变化
        window.addEventListener('resize', handleResize);

        console.log('事件监听器已绑定');
    }

    /**
     * 初始化 TOC
     */
    function init() {
        console.log('初始化浮动 TOC...');

        // 检测设备类型
        isMobile = checkMobile();

        // 提取数据
        tocData.headings = extractHeadings();
        tocData.calibratedSection = findCalibratedSection();

        // 为校对文本区块添加 ID
        if (tocData.calibratedSection && !tocData.calibratedSection.id) {
            tocData.calibratedSection.id = 'calibrated-section';
        }

        // 如果没有任何内容，退出
        if (tocData.headings.length === 0 && !tocData.calibratedSection) {
            console.log('页面没有可用的标题或区块，跳过 TOC 初始化');
            return;
        }

        // 渲染 TOC
        renderTOC();

        // 绑定事件
        bindEvents();

        // 恢复 Pin 状态
        isPinned = loadPinState();
        if (isPinned && !isMobile) {
            const container = document.getElementById('floating-toc');
            const pinBtn = document.getElementById('toc-pin-btn');
            if (container && pinBtn) {
                container.classList.add('pinned');
                container.classList.remove('collapsed');
                pinBtn.classList.add('pinned');
                pinBtn.title = '取消固定目录（已固定）';
            }
        } else {
            // 确保初始状态的 tooltip 正确
            const pinBtn = document.getElementById('toc-pin-btn');
            if (pinBtn) {
                pinBtn.title = '固定目录（点击保持展开）';
            }
        }

        // 设置滚动监听
        setupScrollObserver();

        console.log('浮动 TOC 初始化完成');
    }

    // ========== 启动 ==========

    // 等待 DOM 完全加载后初始化
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        // DOM 已经加载完成
        init();
    }

})();
