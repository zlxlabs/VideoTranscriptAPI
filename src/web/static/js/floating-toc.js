/**
 * Floating TOC (Table of Contents)
 * Responsive design for desktop and mobile.
 *
 * Features:
 * - Auto-extract H1-H4 from the summary section (outline tab)
 * - Chapters are read from the #chapters-data JSON island
 *   (items: {index,title,gist,start_time,start_seg,jump_ok}); a chapter row
 *   shows time + title + full gist and jumps to the inline
 *   #chapter-anchor-{index} header (fallback #dlg-{start_seg})
 * - Chapter pages: panel with "chapters | outline" tabs, current chapter
 *   tracking via IntersectionObserver on .chapter-anchor
 * - Breakpoints on chapter pages:
 *     >=1400px: docked expanded panel, body gets a right margin (toc-wide-margin)
 *     769-1399px: overlay panel, expanded by default, manually collapsible
 *       (state in localStorage key vta_toc_panel_collapsed)
 *     <=768px: sticky current-chapter bar + FAB + bottom drawer
 * - Pages without chapters keep the legacy behavior (collapsed indicator bar,
 *   hover/pin to expand, localStorage key vta_toc_pinned)
 * - Scroll highlight + smooth jump
 * - XSS: build all user/chapter text via DOM API + textContent only
 *   (never HTML-string concatenation of titles)
 */

(function() {
    'use strict';

    // ========== Config ==========
    const CONFIG = {
        STORAGE_KEY: 'vta_toc_pinned',
        COLLAPSE_STORAGE_KEY: 'vta_toc_panel_collapsed',
        OBSERVER_OPTIONS: {
            threshold: 0.5,
            rootMargin: '-100px 0px -60% 0px'
        },
        MOBILE_QUERY: '(max-width: 768px)',
        WIDE_QUERY: '(min-width: 1400px)'
    };

    // ========== State ==========
    let tocData = {
        headings: [],
        calibratedSection: null,
        chapters: []
    };

    let hasChapters = false;
    let observer = null;
    let chapterObserver = null;
    let transcriptObserver = null;
    let isPinned = false;
    let mode = 'mid'; // 'mobile' | 'mid' | 'wide'
    let currentChapterIndex = null;
    let transcriptInView = false;
    let stickyBar = null;
    let stickyBarLabel = null;
    const passedAnchors = new Set();

    const mobileMq = window.matchMedia(CONFIG.MOBILE_QUERY);
    const wideMq = window.matchMedia(CONFIG.WIDE_QUERY);

    // ========== Utils ==========

    function computeMode() {
        if (mobileMq.matches) return 'mobile';
        if (wideMq.matches) return 'wide';
        return 'mid';
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

    function loadCollapseState() {
        try {
            return localStorage.getItem(CONFIG.COLLAPSE_STORAGE_KEY) === 'true';
        } catch (e) {
            console.warn('Failed to load TOC collapse state:', e);
            return false;
        }
    }

    function saveCollapseState(collapsed) {
        try {
            localStorage.setItem(CONFIG.COLLAPSE_STORAGE_KEY, collapsed.toString());
        } catch (e) {
            console.warn('Failed to save TOC collapse state:', e);
        }
    }

    /**
     * Format chapter start seconds as mm:ss (or h:mm:ss), matching the
     * server-side _format_chapter_seconds. Empty string when unknown.
     */
    function formatChapterSeconds(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) {
            return '';
        }
        const total = Math.floor(seconds);
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const secs = total % 60;
        const mm = String(minutes).padStart(2, '0');
        const ss = String(secs).padStart(2, '0');
        return hours > 0 ? (hours + ':' + mm + ':' + ss) : (mm + ':' + ss);
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

    /**
     * Scroll the container just enough to reveal the item; no-op when the
     * item is already fully visible (avoids panel jitter).
     */
    function ensureItemVisible(container, item) {
        if (!container || !item) return;
        const cRect = container.getBoundingClientRect();
        const iRect = item.getBoundingClientRect();
        if (iRect.top >= cRect.top && iRect.bottom <= cRect.bottom) {
            return;
        }
        const delta = iRect.top - cRect.top - (cRect.height - iRect.height) / 2;
        container.scrollTop += delta;
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
     * Read chapters from the #chapters-data JSON island.
     * Only jumpable chapters (jump_ok) get a jump target.
     */
    function readChaptersData() {
        const island = document.getElementById('chapters-data');
        if (!island) {
            return [];
        }

        let raw = null;
        try {
            raw = JSON.parse(island.textContent);
        } catch (e) {
            console.warn('Failed to parse chapters data island:', e);
            return [];
        }
        if (!Array.isArray(raw)) {
            return [];
        }

        const chapters = [];
        raw.forEach((ch) => {
            if (!ch || typeof ch !== 'object') return;

            let index = parseInt(ch.index, 10);
            if (isNaN(index)) index = chapters.length;

            const title = (typeof ch.title === 'string' ? ch.title : '').trim();
            if (!title) return;

            const gist = typeof ch.gist === 'string' ? ch.gist : '';

            let startSeg = parseInt(ch.start_seg, 10);
            if (isNaN(startSeg)) startSeg = null;

            const jumpOk = ch.jump_ok === true && startSeg !== null;
            const anchorId = jumpOk ? ('chapter-anchor-' + index) : null;

            chapters.push({
                index: index,
                title: title,
                gist: gist,
                timeLabel: formatChapterSeconds(ch.start_time),
                startSeg: startSeg,
                jumpOk: jumpOk,
                anchorId: anchorId,
                dlgId: jumpOk ? ('dlg-' + startSeg) : null,
                anchorEl: anchorId ? document.getElementById(anchorId) : null
            });
        });

        console.log('Loaded chapters: ' + chapters.length);
        return chapters;
    }

    // ========== UI (DOM API) ==========

    function buildOutlineList(listEl, showSectionTitles) {
        const headings = tocData.headings;

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

        if (tocData.calibratedSection) {
            appendTocLink(listEl, {
                href: '#calibrated-section',
                text: '校对文本',
                id: 'calibrated-section',
                className: 'toc-link toc-anchor'
            });
        }
    }

    /**
     * One chapter row, shared by the PC panel and the mobile drawer.
     * Time + title row (whole-row jump) with the full gist text below it.
     * Gists are short, so the gist is plain inert text: no clamping and
     * no expand/collapse interaction.
     */
    function buildChapterItem(chapter) {
        const item = createEl('div', 'toc-chapter-item');
        item.dataset.chapterIndex = String(chapter.index);
        if (!chapter.jumpOk) {
            item.classList.add('toc-chapter-disabled');
        }

        const main = createEl('button', 'toc-chapter-main');
        main.type = 'button';
        if (chapter.jumpOk) {
            main.dataset.targetId = chapter.anchorId || chapter.dlgId;
            main.dataset.fallbackId = chapter.dlgId || '';
        }
        if (chapter.timeLabel) {
            main.appendChild(createEl('span', 'toc-chapter-time', chapter.timeLabel));
        }
        main.appendChild(createEl('span', 'toc-chapter-title', chapter.title));
        item.appendChild(main);

        if (chapter.gist) {
            item.appendChild(createEl('div', 'toc-chapter-gist', chapter.gist));
        }

        return item;
    }

    function buildChaptersPane(isActive) {
        const pane = createEl('div', 'toc-pane toc-chapters-pane' + (isActive ? ' active' : ''));
        tocData.chapters.forEach((chapter) => {
            pane.appendChild(buildChapterItem(chapter));
        });
        return pane;
    }

    function buildOutlinePane(isActive, showSectionTitles) {
        const pane = createEl('div', 'toc-pane toc-outline-pane' + (isActive ? ' active' : ''));
        const list = createEl('ul', 'toc-list');
        buildOutlineList(list, showSectionTitles);
        pane.appendChild(list);
        return pane;
    }

    function buildTabs() {
        const tabs = createEl('div', 'toc-tabs');
        tabs.setAttribute('role', 'tablist');

        const chapterTab = createEl('button', 'toc-tab', '章节');
        chapterTab.type = 'button';
        chapterTab.dataset.tab = 'chapters';
        chapterTab.setAttribute('role', 'tab');
        chapterTab.classList.add('active'); // chapters tab is the default

        const outlineTab = createEl('button', 'toc-tab', '大纲');
        outlineTab.type = 'button';
        outlineTab.dataset.tab = 'outline';
        outlineTab.setAttribute('role', 'tab');

        tabs.appendChild(chapterTab);
        tabs.appendChild(outlineTab);
        return tabs;
    }

    /**
     * Switch tabs within one widget root (PC container or mobile drawer).
     */
    function setActiveTab(rootEl, tabName) {
        rootEl.querySelectorAll('.toc-tab').forEach(tab => {
            tab.classList.toggle('active', tab.dataset.tab === tabName);
        });
        rootEl.querySelectorAll('.toc-chapters-pane').forEach(pane => {
            pane.classList.toggle('active', tabName === 'chapters');
        });
        rootEl.querySelectorAll('.toc-outline-pane').forEach(pane => {
            pane.classList.toggle('active', tabName === 'outline');
        });

        // When switching back to chapters, keep the current chapter visible.
        if (tabName === 'chapters' && currentChapterIndex !== null) {
            const scroller = rootEl.querySelector('.toc-content') || rootEl.querySelector('.toc-mobile-body');
            const item = rootEl.querySelector('.toc-chapter-item.current');
            if (scroller && item) {
                ensureItemVisible(scroller, item);
            }
        }
    }

    function createPCToc() {
        const container = createEl('div', 'floating-toc-container');
        container.id = 'floating-toc';

        if (hasChapters) {
            container.classList.add('toc-new');
        } else {
            container.classList.add('collapsed');
        }

        const indicator = createEl('div', 'toc-indicator');
        for (let i = 0; i < 4; i++) {
            indicator.appendChild(createEl('div', 'toc-indicator-line'));
        }
        container.appendChild(indicator);

        const header = createEl('div', 'toc-header');
        if (hasChapters) {
            header.appendChild(buildTabs());
            const collapseBtn = createEl('button', 'toc-collapse-btn');
            collapseBtn.id = 'toc-collapse-btn';
            collapseBtn.type = 'button';
            collapseBtn.title = '收起目录';
            collapseBtn.textContent = '»';
            header.appendChild(collapseBtn);
        } else {
            header.appendChild(createEl('div', 'toc-title', '目录'));
            const pinBtn = createEl('button', 'toc-pin-btn');
            pinBtn.id = 'toc-pin-btn';
            pinBtn.title = '固定目录（点击保持展开）';
            pinBtn.type = 'button';
            pinBtn.textContent = '📌';
            header.appendChild(pinBtn);
        }
        container.appendChild(header);

        const content = createEl('div', 'toc-content');
        if (hasChapters) {
            content.appendChild(buildChaptersPane(true));
            content.appendChild(buildOutlinePane(false, true));
        } else {
            const list = createEl('ul', 'toc-list');
            buildOutlineList(list, true);
            content.appendChild(list);
        }
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
        if (hasChapters) {
            mobileHeader.appendChild(buildTabs());
        } else {
            mobileHeader.appendChild(createEl('div', 'toc-mobile-title', '目录'));
        }
        const closeBtn = createEl('button', 'toc-mobile-close-btn');
        closeBtn.id = 'toc-mobile-close-btn';
        closeBtn.type = 'button';
        closeBtn.textContent = '✕';
        mobileHeader.appendChild(closeBtn);
        mobileContent.appendChild(mobileHeader);

        const body = createEl('div', 'toc-mobile-body');
        if (hasChapters) {
            body.appendChild(buildChaptersPane(true));
            body.appendChild(buildOutlinePane(false, true));
        } else {
            const list = createEl('ul', 'toc-list');
            buildOutlineList(list, true);
            body.appendChild(list);
        }
        mobileContent.appendChild(body);
        panel.appendChild(mobileContent);

        return { btn: btn, panel: panel };
    }

    /**
     * Sticky current-chapter bar (mobile only). Created once, JS toggles the
     * hidden attribute and the label text.
     */
    function createStickyBar() {
        const bar = createEl('div', 'chapter-sticky-bar');
        bar.setAttribute('role', 'button');
        bar.hidden = true;
        stickyBarLabel = createEl('span', 'chapter-sticky-bar-label');
        bar.appendChild(stickyBarLabel);
        bar.appendChild(createEl('span', 'chapter-sticky-bar-icon', '☰'));
        stickyBar = bar;
        return bar;
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
        const existingSticky = document.querySelector('.chapter-sticky-bar');

        if (existingPC) existingPC.remove();
        if (existingMobileBtn) existingMobileBtn.remove();
        if (existingMobilePanel) existingMobilePanel.remove();
        if (existingSticky) existingSticky.remove();
        stickyBar = null;
        stickyBarLabel = null;

        if (!hasTocContent()) {
            console.log('No TOC data, skip render');
            return;
        }

        // Pure DOM append — no insertAdjacentHTML for titles
        document.body.appendChild(createPCToc());
        const mobile = createMobileTocParts();
        document.body.appendChild(mobile.btn);
        document.body.appendChild(mobile.panel);
        if (hasChapters) {
            document.body.appendChild(createStickyBar());
        }

        console.log('TOC render complete');
    }

    // ========== Mode / breakpoints ==========

    function applyCollapsed(container, collapsed) {
        container.classList.toggle('toc-collapsed', collapsed);
        const btn = container.querySelector('#toc-collapse-btn');
        if (btn) {
            btn.textContent = collapsed ? '«' : '»';
            btn.title = collapsed ? '展开目录' : '收起目录';
        }
    }

    function applyMode() {
        const prevMode = mode;
        mode = computeMode();

        if (prevMode === 'mobile' && mode !== 'mobile') {
            closeMobilePanel();
        }

        if (hasChapters) {
            const container = document.getElementById('floating-toc');
            if (container) {
                if (mode === 'wide') {
                    container.classList.add('toc-docked');
                    applyCollapsed(container, false);
                } else {
                    container.classList.remove('toc-docked');
                    applyCollapsed(container, mode === 'mid' && loadCollapseState());
                }
            }
        }

        document.body.classList.toggle('toc-wide-margin', hasChapters && mode === 'wide');
        updateStickyBar();
        scrollPanelToCurrentChapter();
    }

    function setupBreakpointListeners() {
        const onChange = () => applyMode();
        if (typeof mobileMq.addEventListener === 'function') {
            mobileMq.addEventListener('change', onChange);
            wideMq.addEventListener('change', onChange);
        } else if (typeof mobileMq.addListener === 'function') {
            mobileMq.addListener(onChange);
            wideMq.addListener(onChange);
        }
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

        if (mode === 'mobile') {
            closeMobilePanel();
        }

        setTimeout(() => {
            updateActiveLink(targetId);
        }, 100);
    }

    function handleChapterJump(mainEl) {
        const targetId = mainEl.dataset.targetId;
        if (!targetId) {
            // Chapter is not jumpable (fingerprint mismatch / no anchors).
            return;
        }

        let targetElement = document.getElementById(targetId);
        if (!targetElement && mainEl.dataset.fallbackId) {
            targetElement = document.getElementById(mainEl.dataset.fallbackId);
        }
        if (!targetElement) {
            console.warn('Chapter jump target not found:', targetId);
            return;
        }

        targetElement.scrollIntoView({
            behavior: 'smooth',
            block: 'start'
        });

        if (mode === 'mobile') {
            closeMobilePanel();
        }
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

    function handleCollapseToggle() {
        // Manual collapse only applies to the mid breakpoint; the wide
        // breakpoint keeps the panel docked/expanded.
        if (mode !== 'mid') return;

        const container = document.getElementById('floating-toc');
        if (!container) return;

        const collapsed = !container.classList.contains('toc-collapsed');
        applyCollapsed(container, collapsed);
        saveCollapseState(collapsed);
    }

    function openMobilePanel() {
        const panel = document.getElementById('toc-mobile-panel');
        if (!panel) return;

        panel.classList.add('show');
        document.body.style.overflow = 'hidden';

        // Reveal the current chapter row when the drawer opens.
        if (hasChapters && currentChapterIndex !== null) {
            const body = panel.querySelector('.toc-mobile-body');
            const item = panel.querySelector('.toc-chapter-item.current');
            if (body && item) {
                ensureItemVisible(body, item);
            }
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

    // ========== Current chapter tracking ==========

    function updateStickyBar() {
        if (!stickyBar) return;

        const show = hasChapters
            && mode === 'mobile'
            && transcriptInView
            && currentChapterIndex !== null;

        if (!show) {
            stickyBar.hidden = true;
            return;
        }

        const chapter = tocData.chapters.find(c => c.index === currentChapterIndex);
        if (!chapter) {
            stickyBar.hidden = true;
            return;
        }

        if (stickyBarLabel) {
            stickyBarLabel.textContent = (chapter.index + 1) + '. ' + chapter.title;
        }
        stickyBar.hidden = false;
    }

    function scrollPanelToCurrentChapter() {
        if (currentChapterIndex === null || mode === 'mobile') return;
        const container = document.getElementById('floating-toc');
        if (!container) return;
        const pane = container.querySelector('.toc-chapters-pane');
        if (!pane || !pane.classList.contains('active')) return;
        const content = container.querySelector('.toc-content');
        const item = pane.querySelector('.toc-chapter-item.current');
        if (content && item) {
            ensureItemVisible(content, item);
        }
    }

    function setCurrentChapter(index) {
        const changed = currentChapterIndex !== index;
        currentChapterIndex = index;

        document.querySelectorAll('.toc-chapter-item').forEach(item => {
            const match = index !== null && item.dataset.chapterIndex === String(index);
            item.classList.toggle('current', match);
        });

        if (changed) {
            scrollPanelToCurrentChapter();
        }
        updateStickyBar();
    }

    function setupChapterObserver() {
        if (!hasChapters) return;

        const indexByEl = new Map();
        tocData.chapters.forEach(ch => {
            if (ch.anchorEl) {
                indexByEl.set(ch.anchorEl, ch.index);
            }
        });
        if (indexByEl.size === 0) return;

        chapterObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                const idx = indexByEl.get(entry.target);
                if (idx === undefined) return;
                // Anchor in view, or already scrolled above the viewport top,
                // means its chapter has been reached.
                if (entry.isIntersecting || entry.boundingClientRect.top < 0) {
                    passedAnchors.add(idx);
                } else {
                    passedAnchors.delete(idx);
                }
            });

            if (passedAnchors.size > 0) {
                setCurrentChapter(Math.max.apply(null, Array.from(passedAnchors)));
            } else {
                setCurrentChapter(null);
            }
        }, { threshold: 0 });

        indexByEl.forEach((idx, el) => {
            chapterObserver.observe(el);
        });

        console.log('Chapter observer ready');
    }

    function setupTranscriptObserver() {
        if (!hasChapters) return;

        const block = document.getElementById('calibrated-content-block');
        if (!block) return;

        transcriptObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                transcriptInView = entry.isIntersecting;
            });
            updateStickyBar();
        }, { threshold: 0 });

        transcriptObserver.observe(block);
    }

    // ========== Scroll observer (outline) ==========

    function setupScrollObserver() {
        if (observer) {
            observer.disconnect();
        }

        const elements = tocData.headings.map(h => h.element);
        if (tocData.calibratedSection) {
            elements.push(tocData.calibratedSection);
        }

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

    // ========== Init ==========

    function bindEvents() {
        document.addEventListener('click', (e) => {
            if (e.target.closest('#toc-pin-btn')) {
                handlePinClick();
                return;
            }

            if (e.target.closest('#toc-collapse-btn')) {
                handleCollapseToggle();
                return;
            }

            // Collapsed chapter panel: clicking the indicator bar re-expands.
            if (e.target.closest('#floating-toc.toc-collapsed .toc-indicator')) {
                handleCollapseToggle();
                return;
            }

            if (e.target.closest('.toc-tab')) {
                const tab = e.target.closest('.toc-tab');
                const root = tab.closest('#floating-toc, #toc-mobile-panel');
                if (root) {
                    setActiveTab(root, tab.dataset.tab);
                }
                return;
            }

            // Whole chapter row is one jump target: title row or gist.
            // Route through the row's .toc-chapter-main (it carries the
            // jump dataset; jump_ok=false rows have none and no-op).
            if (e.target.closest('.toc-chapter-main, .toc-chapter-gist')) {
                const item = e.target.closest('.toc-chapter-item');
                const main = item ? item.querySelector('.toc-chapter-main') : null;
                if (main) {
                    handleChapterJump(main);
                }
                return;
            }

            if (e.target.closest('.chapter-sticky-bar')) {
                openMobilePanel();
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

        console.log('TOC events bound');
    }

    function init() {
        console.log('Init floating TOC...');

        mode = computeMode();

        tocData.headings = extractHeadings();
        tocData.calibratedSection = findCalibratedSection();
        tocData.chapters = readChaptersData();
        hasChapters = tocData.chapters.length > 0;

        if (tocData.calibratedSection && !tocData.calibratedSection.id) {
            tocData.calibratedSection.id = 'calibrated-section';
        }

        if (!hasTocContent()) {
            console.log('No headings/chapters/calibrated section; skip TOC');
            return;
        }

        renderTOC();
        bindEvents();
        applyMode();

        if (!hasChapters) {
            // Legacy behavior: collapsed indicator bar, pin to keep expanded.
            isPinned = loadPinState();
            if (isPinned && mode !== 'mobile') {
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
        }

        setupScrollObserver();
        setupChapterObserver();
        setupTranscriptObserver();
        setupBreakpointListeners();

        console.log('Floating TOC ready');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
