/**
 * Floating TOC (Table of Contents)
 * Responsive design for desktop and mobile.
 *
 * Features:
 * - Auto-extract H1-H4 from the summary section
 * - Chapters group: jump to #dlg-{start_seg} when data-jump-ok=1
 * - Desktop: right-side floating panel with pin
 * - Mobile: bottom FAB + half-screen panel
 * - Scroll highlight + smooth jump
 * - XSS: build all user/chapter titles via DOM API + textContent
 *   (never insertAdjacentHTML / innerHTML string concat of titles)
 */

(function() {
    'use strict';

    // ========== Config ==========
    const CONFIG = {
        STORAGE_KEY: 'vta_toc_pinned',
        OBSERVER_OPTIONS: {
            threshold: 0.5,
            rootMargin: '-100px 0px -60% 0px'
        },
        MOBILE_BREAKPOINT: 768
    };

    // ========== State ==========
    let tocData = {
        headings: [],
        calibratedSection: null,
        chapters: []
    };

    let observer = null;
    let isPinned = false;
    let isMobile = false;

    // ========== Utils ==========

    function checkMobile() {
        return window.innerWidth <= CONFIG.MOBILE_BREAKPOINT;
    }

    function generateId(text, index) {
        const slug = text
            .toLowerCase()
            .replace(/[^\w\s\u4e00-\u9fa5-]/g, '')
            .replace(/\s+/g, '-')
            .substring(0, 50);
        return `toc-heading-${slug}-${index}`;
    }

    function loadPinState() {
        try {
            const state = localStorage.getItem(CONFIG.STORAGE_KEY);
            return state === 'true';
        } catch (e) {
            console.warn('Failed to load TOC pin state:', e);
            return false;
        }
    }

    function savePinState(pinned) {
        try {
            localStorage.setItem(CONFIG.STORAGE_KEY, pinned.toString());
        } catch (e) {
            console.warn('Failed to save TOC pin state:', e);
        }
    }

    /**
     * Create an element with optional className and safe textContent.
     */
    function createEl(tag, className, text) {
        const el = document.createElement(tag);
        if (className) {
            el.className = className;
        }
        if (text != null && text !== '') {
            el.textContent = text;
        }
        return el;
    }

    /**
     * Append a TOC link item using DOM API only (textContent for labels).
     */
    function appendTocLink(listEl, options) {
        const item = createEl('div', 'toc-item');
        const link = createEl('a', options.className || 'toc-link');
        link.setAttribute('href', options.href || '#');
        if (options.id != null) {
            link.dataset.id = String(options.id);
        }
        if (options.level != null) {
            link.setAttribute('data-level', String(options.level));
        }
        // XSS-safe: never interpolate user text into HTML strings
        link.textContent = options.text || '';
        item.appendChild(link);
        listEl.appendChild(item);
        return link;
    }

    function appendSectionTitle(listEl, text) {
        const title = createEl('div', 'toc-section-title', text);
        listEl.appendChild(title);
    }

    // ========== Data extraction ==========

    function extractHeadings() {
        const headings = [];

        const summarySection = Array.from(document.querySelectorAll('.section')).find(section => {
            const h2 = section.querySelector('h2');
            return h2 && h2.textContent.includes('内容总结');
        });

        if (!summarySection) {
            console.warn('Summary section not found');
            return headings;
        }

        const contentDiv = summarySection.querySelector('.content');
        if (!contentDiv) {
            console.warn('Summary content area not found');
            return headings;
        }

        const headingElements = contentDiv.querySelectorAll('h1, h2, h3, h4');

        headingElements.forEach((element, index) => {
            const level = parseInt(element.tagName.substring(1), 10);
            const text = element.textContent.trim();

            if (!text) return;

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

        console.log('Extracted headings: ' + headings.length);
        return headings;
    }

    function findCalibratedSection() {
        const sections = Array.from(document.querySelectorAll('.section'));
        return sections.find(section => {
            const h2 = section.querySelector('h2');
            return h2 && h2.textContent.includes('校对文本');
        }) || null;
    }

    /**
     * Extract chapters from the server-rendered chapters section.
     * Only jumpable chapters (data-jump-ok=1) get a #dlg-{start_seg} target.
     */
    function extractChapters() {
        const section = document.getElementById('chapters-section');
        if (!section) {
            return [];
        }

        const cards = section.querySelectorAll('.chapter-card');
        const chapters = [];

        cards.forEach((card) => {
            const startSeg = card.getAttribute('data-start-seg');
            const jumpOk = card.getAttribute('data-jump-ok') === '1';
            const titleEl = card.querySelector('.chapter-title-link, .chapter-title');
            const text = titleEl ? titleEl.textContent.trim() : '';
            if (!text) return;

            const dlgId = (jumpOk && startSeg !== null && startSeg !== '')
                ? ('dlg-' + startSeg)
                : null;

            chapters.push({
                text: text,
                id: dlgId,
                startSeg: startSeg,
                jumpOk: jumpOk,
                element: dlgId ? document.getElementById(dlgId) : null
            });
        });

        console.log('Extracted chapters: ' + chapters.length);
        return chapters;
    }

    // ========== UI (DOM API) ==========

    function buildTocList(listEl, options) {
        const headings = tocData.headings;
        const hasCalibratedSection = !!tocData.calibratedSection;
        const chapters = tocData.chapters;
        const showSectionTitles = !!options.showSectionTitles;

        if (headings.length > 0) {
            if (showSectionTitles) {
                appendSectionTitle(listEl, '内容总结');
            }
            headings.forEach(heading => {
                appendTocLink(listEl, {
                    href: '#' + heading.id,
                    text: heading.text,
                    level: heading.level,
                    id: heading.id,
                    className: 'toc-link'
                });
            });
        }

        if (chapters.length > 0) {
            if (showSectionTitles) {
                appendSectionTitle(listEl, '章节梗概');
            }
            chapters.forEach((chapter, idx) => {
                if (chapter.jumpOk && chapter.id) {
                    appendTocLink(listEl, {
                        href: '#' + chapter.id,
                        text: chapter.text,
                        id: chapter.id,
                        className: 'toc-link toc-chapter'
                    });
                } else {
                    // Fingerprint mismatch: show label without jump
                    const item = createEl('div', 'toc-item');
                    const span = createEl('span', 'toc-link toc-chapter toc-nolink', chapter.text);
                    item.appendChild(span);
                    listEl.appendChild(item);
                }
            });
        }

        if (hasCalibratedSection) {
            appendTocLink(listEl, {
                href: '#calibrated-section',
                text: '校对文本',
                id: 'calibrated-section',
                className: 'toc-link toc-anchor'
            });
        }
    }

    function createPCToc() {
        const container = createEl('div', 'floating-toc-container collapsed');
        container.id = 'floating-toc';

        const indicator = createEl('div', 'toc-indicator');
        for (let i = 0; i < 4; i++) {
            indicator.appendChild(createEl('div', 'toc-indicator-line'));
        }
        container.appendChild(indicator);

        const header = createEl('div', 'toc-header');
        header.appendChild(createEl('div', 'toc-title', '目录'));
        const pinBtn = createEl('button', 'toc-pin-btn');
        pinBtn.id = 'toc-pin-btn';
        pinBtn.title = '固定目录（点击保持展开）';
        pinBtn.type = 'button';
        pinBtn.textContent = '📌';
        header.appendChild(pinBtn);
        container.appendChild(header);

        const content = createEl('div', 'toc-content');
        const list = createEl('ul', 'toc-list');
        // Use div children (existing CSS targets .toc-item inside .toc-list)
        buildTocList(list, { showSectionTitles: true });
        content.appendChild(list);
        container.appendChild(content);

        return container;
    }

    function createMobileTocParts() {
        const btn = createEl('button', 'floating-toc-mobile-btn');
        btn.id = 'toc-mobile-btn';
        btn.title = '目录';
        btn.type = 'button';
        btn.textContent = '📑';

        const panel = createEl('div', 'floating-toc-mobile-panel');
        panel.id = 'toc-mobile-panel';

        const overlay = createEl('div', 'toc-mobile-overlay');
        overlay.id = 'toc-mobile-overlay';
        panel.appendChild(overlay);

        const mobileContent = createEl('div', 'toc-mobile-content');
        const mobileHeader = createEl('div', 'toc-mobile-header');
        mobileHeader.appendChild(createEl('div', 'toc-mobile-title', '目录'));
        const closeBtn = createEl('button', 'toc-mobile-close-btn');
        closeBtn.id = 'toc-mobile-close-btn';
        closeBtn.type = 'button';
        closeBtn.textContent = '✕';
        mobileHeader.appendChild(closeBtn);
        mobileContent.appendChild(mobileHeader);

        const body = createEl('div', 'toc-mobile-body');
        const list = createEl('ul', 'toc-list');
        buildTocList(list, { showSectionTitles: true });
        body.appendChild(list);
        mobileContent.appendChild(body);
        panel.appendChild(mobileContent);

        return { btn: btn, panel: panel };
    }

    function hasTocContent() {
        return tocData.headings.length > 0
            || !!tocData.calibratedSection
            || tocData.chapters.length > 0;
    }

    function renderTOC() {
        const existingPC = document.getElementById('floating-toc');
        const existingMobileBtn = document.getElementById('toc-mobile-btn');
        const existingMobilePanel = document.getElementById('toc-mobile-panel');

        if (existingPC) existingPC.remove();
        if (existingMobileBtn) existingMobileBtn.remove();
        if (existingMobilePanel) existingMobilePanel.remove();

        if (!hasTocContent()) {
            console.log('No TOC data, skip render');
            return;
        }

        // Pure DOM append — no insertAdjacentHTML for titles
        document.body.appendChild(createPCToc());
        const mobile = createMobileTocParts();
        document.body.appendChild(mobile.btn);
        document.body.appendChild(mobile.panel);

        console.log('TOC render complete');
    }

    // ========== Events ==========

    function handleTocClick(e) {
        const link = e.target.closest('.toc-link');
        if (!link || link.tagName !== 'A') return;

        e.preventDefault();

        const targetId = link.dataset.id;
        let targetElement = null;

        if (targetId === 'calibrated-section') {
            targetElement = tocData.calibratedSection;
        } else if (targetId) {
            targetElement = document.getElementById(targetId);
        }

        if (!targetElement) {
            console.warn('TOC target not found:', targetId);
            return;
        }

        targetElement.scrollIntoView({
            behavior: 'smooth',
            block: 'start'
        });

        if (isMobile) {
            closeMobilePanel();
        }

        setTimeout(() => {
            updateActiveLink(targetId);
        }, 100);
    }

    function handlePinClick() {
        const container = document.getElementById('floating-toc');
        const pinBtn = document.getElementById('toc-pin-btn');

        if (!container || !pinBtn) return;

        isPinned = !isPinned;

        if (isPinned) {
            pinBtn.classList.add('animating-pin');
            setTimeout(() => {
                pinBtn.classList.remove('animating-pin');
            }, 400);

            container.classList.add('pinned');
            container.classList.remove('collapsed');
            pinBtn.classList.add('pinned');
            pinBtn.title = '取消固定目录（已固定）';
        } else {
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

    function openMobilePanel() {
        const panel = document.getElementById('toc-mobile-panel');
        if (panel) {
            panel.classList.add('show');
            document.body.style.overflow = 'hidden';
        }
    }

    function closeMobilePanel() {
        const panel = document.getElementById('toc-mobile-panel');
        if (panel) {
            panel.classList.remove('show');
            document.body.style.overflow = '';
        }
    }

    function updateActiveLink(activeId) {
        const links = document.querySelectorAll('.toc-link');
        links.forEach(link => {
            if (link.dataset && link.dataset.id === activeId) {
                link.classList.add('active');
            } else {
                link.classList.remove('active');
            }
        });
    }

    // ========== Scroll observer ==========

    function setupScrollObserver() {
        if (observer) {
            observer.disconnect();
        }

        const elements = tocData.headings.map(h => h.element);
        if (tocData.calibratedSection) {
            elements.push(tocData.calibratedSection);
        }
        tocData.chapters.forEach(ch => {
            if (ch.element) {
                elements.push(ch.element);
            }
        });

        if (elements.length === 0) return;

        observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const id = entry.target.id || 'calibrated-section';
                    updateActiveLink(id);
                }
            });
        }, CONFIG.OBSERVER_OPTIONS);

        elements.forEach(element => {
            if (element) observer.observe(element);
        });

        console.log('Scroll observer ready');
    }

    // ========== Responsive ==========

    function handleResize() {
        const wasMobile = isMobile;
        isMobile = checkMobile();

        if (wasMobile && !isMobile) {
            closeMobilePanel();
        }
    }

    // ========== Init ==========

    function bindEvents() {
        document.addEventListener('click', (e) => {
            if (e.target.closest('#toc-pin-btn')) {
                handlePinClick();
                return;
            }

            if (e.target.closest('#toc-mobile-btn')) {
                openMobilePanel();
                return;
            }

            if (e.target.closest('#toc-mobile-close-btn')) {
                e.preventDefault();
                e.stopPropagation();
                closeMobilePanel();
                return;
            }

            if (e.target.closest('#toc-mobile-overlay')) {
                closeMobilePanel();
                return;
            }

            if (e.target.closest('a.toc-link')) {
                handleTocClick(e);
                return;
            }
        });

        window.addEventListener('resize', handleResize);

        console.log('TOC events bound');
    }

    function init() {
        console.log('Init floating TOC...');

        isMobile = checkMobile();

        tocData.headings = extractHeadings();
        tocData.calibratedSection = findCalibratedSection();
        tocData.chapters = extractChapters();

        if (tocData.calibratedSection && !tocData.calibratedSection.id) {
            tocData.calibratedSection.id = 'calibrated-section';
        }

        if (!hasTocContent()) {
            console.log('No headings/chapters/calibrated section; skip TOC');
            return;
        }

        renderTOC();
        bindEvents();

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
            const pinBtn = document.getElementById('toc-pin-btn');
            if (pinBtn) {
                pinBtn.title = '固定目录（点击保持展开）';
            }
        }

        setupScrollObserver();

        console.log('Floating TOC ready');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
