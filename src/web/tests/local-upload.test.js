// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const indexSource = readFileSync('src/web/static/index.html', 'utf-8');
const authSource = readFileSync('src/web/static/js/auth-storage.js', 'utf-8');
const appSource = readFileSync('src/web/static/js/app.js', 'utf-8');
const historySource = readFileSync('src/web/static/history.html', 'utf-8');

let dom;

function decodeBase64Url(str) {
  const padded = str + '='.repeat((4 - (str.length % 4)) % 4);
  const base64 = padded.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(base64);
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

async function installIndexPage({ token = 'test-token', capabilities = null } = {}) {
  dom = new JSDOM(indexSource, {
    url: 'https://vta.test/add_task_by_web',
    runScripts: 'outside-only',
  });
  const { window } = dom;
  dom.window.HTMLElement.prototype.scrollIntoView = vi.fn();
  window.matchMedia = vi.fn((media) => ({
    matches: false,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));

  const caps = capabilities || {
    enabled: true,
    default_retention: '30d',
    retention_options: ['30d', 'never'],
    limits: {
      max_file_mib: 100,
      max_media_hours: 2,
      receive_concurrency: 1,
      upload_temp_budget_mib: 500,
    },
  };

  window.fetch = vi.fn(async (url, options = {}) => {
    if (typeof url === 'string' && url.includes('/api/uploads/capabilities')) {
      return {
        ok: true,
        status: 200,
        json: async () => caps,
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ code: 200, data: {} }),
    };
  });

  window.eval(authSource);
  if (token) {
    window.VideoTranscriptAuthStorage.writeAuthToken(token, { remember: true });
  }
  window.eval(appSource);

  await new Promise((resolve) => {
    window.addEventListener('DOMContentLoaded', resolve, { once: true });
  });

  // Allow microtasks and timers
  await new Promise((resolve) => setTimeout(resolve, 10));
}

afterEach(() => {
  vi.clearAllTimers();
  dom?.window?.close();
  vi.restoreAllMocks();
});

describe('local upload frontend logic', () => {
  it('correctly constructs metadata with exactly 6 required fields and base64url encoding', async () => {
    await installIndexPage();
    const { window } = dom;

    const file = new window.File(['fake audio content'], '测试录音.mp3', { type: 'audio/mp3' });
    const metadata = window.buildUploadMetadata(file, {
      title: '中文标题测试',
      sourceUrl: 'https://example.com/source',
      retention: '30d',
      processingOptions: {
        calibrate: true,
        summarize: true,
        infer_speaker_names: false,
        chapters: true,
      },
    });

    const expectedKeys = ['filename', 'byte_size', 'title', 'source_url', 'retention', 'processing_options'];
    expect(Object.keys(metadata).sort()).toEqual(expectedKeys.sort());
    expect(metadata.filename).toBe('测试录音.mp3');
    expect(metadata.byte_size).toBe(file.size);
    expect(metadata.title).toBe('中文标题测试');
    expect(metadata.source_url).toBe('https://example.com/source');
    expect(metadata.retention).toBe('30d');
    expect(metadata.processing_options.infer_speaker_names).toBe(false);

    const encoded = window.encodeUploadMetadata(metadata);
    expect(encoded).not.toContain('+');
    expect(encoded).not.toContain('/');
    expect(encoded).not.toContain('=');

    const decoded = decodeBase64Url(encoded);
    expect(decoded).toEqual(metadata);
  });

  it('sets optional title and source_url to null when empty', async () => {
    await installIndexPage();
    const { window } = dom;

    const file = new window.File(['sample'], 'sample.wav', { type: 'audio/wav' });
    const metadata = window.buildUploadMetadata(file, {
      title: '   ',
      sourceUrl: '',
      retention: '30d',
      processingOptions: { calibrate: true, summarize: false, infer_speaker_names: false, chapters: false },
    });

    expect(metadata.title).toBeNull();
    expect(metadata.source_url).toBeNull();
    expect(metadata.processing_options.summarize).toBe(false);
  });

  it('generates valid Idempotency-Key adhering to epoch_ms-UUID format', async () => {
    await installIndexPage();
    const { window } = dom;

    const key = window.generateUploadIdempotencyKey();
    expect(key).toMatch(/^\d+-[0-9a-fA-F-]+$/);
  });

  it('switches between URL mode and Upload mode without breaking URL transcription', async () => {
    await installIndexPage();
    const { document } = dom.window;

    const uploadTab = document.getElementById('mode-tab-upload');
    const urlTab = document.getElementById('mode-tab-url');
    const urlPanel = document.getElementById('url-mode-panel');
    const uploadPanel = document.getElementById('upload-mode-panel');

    expect(uploadTab).toBeTruthy();
    expect(urlTab).toBeTruthy();

    // Default is URL mode
    expect(urlPanel.hidden).toBe(false);
    expect(uploadPanel.hidden).toBe(true);

    // Switch to upload mode
    uploadTab.click();
    expect(urlPanel.hidden).toBe(true);
    expect(uploadPanel.hidden).toBe(false);

    // Switch back to URL mode
    urlTab.click();
    expect(urlPanel.hidden).toBe(false);
    expect(uploadPanel.hidden).toBe(true);
  });

  it('disables upload submission and shows notice when capabilities.enabled is false', async () => {
    await installIndexPage({
      capabilities: {
        enabled: false,
        default_retention: '30d',
        retention_options: ['30d', 'never'],
        limits: { max_file_mib: 100, max_media_hours: 2, receive_concurrency: 1, upload_temp_budget_mib: 500 },
      },
    });
    const { window } = dom;
    const { document } = window;

    document.getElementById('mode-tab-upload').click();
    const banner = document.getElementById('upload-disabled-banner');
    expect(banner.hidden).toBe(false);

    const file = new window.File(['abc'], 'clip.mp4', { type: 'video/mp4' });
    window.selectUploadFile(file);

    const submitBtn = document.getElementById('submit-btn');
    expect(submitBtn.disabled).toBe(true);
  });

  it('rejects files exceeding limits.max_file_mib', async () => {
    await installIndexPage({
      capabilities: {
        enabled: true,
        default_retention: '30d',
        retention_options: ['30d', 'never'],
        limits: { max_file_mib: 1, max_media_hours: 1, receive_concurrency: 1, upload_temp_budget_mib: 10 },
      },
    });
    const { window } = dom;
    const { document } = window;
    document.getElementById('mode-tab-upload').click();

    // 2 MiB file exceeding 1 MiB limit
    const largeContent = new Uint8Array(2 * 1024 * 1024);
    const file = new window.File([largeContent], 'large.mp4', { type: 'video/mp4' });

    window.selectUploadFile(file);
    const feedback = document.getElementById('upload-input-feedback');
    expect(feedback.hidden).toBe(false);
    expect(feedback.textContent).toContain('超出限制');

    const submitBtn = document.getElementById('submit-btn');
    expect(submitBtn.disabled).toBe(true);
  });

  it('resets retention to 30d when a new file is chosen', async () => {
    await installIndexPage();
    const { window } = dom;
    const { document } = window;
    document.getElementById('mode-tab-upload').click();

    const file1 = new window.File(['abc'], 'clip1.mp4', { type: 'video/mp4' });
    window.selectUploadFile(file1);

    // Explicitly choose never
    const neverRadio = document.querySelector('input[name="upload-retention"][value="never"]');
    neverRadio.checked = true;
    neverRadio.dispatchEvent(new window.Event('change'));
    expect(window.getUploadRetention()).toBe('never');

    // Select a second file
    const file2 = new window.File(['def'], 'clip2.mp4', { type: 'video/mp4' });
    window.selectUploadFile(file2);

    // Must reset to 30d
    expect(window.getUploadRetention()).toBe('30d');
    const defaultRadio = document.querySelector('input[name="upload-retention"][value="30d"]');
    expect(defaultRadio.checked).toBe(true);
  });

  it('prevents double submission on duplicate clicks for same upload intent', async () => {
    await installIndexPage();
    const { window } = dom;
    const { document } = window;
    document.getElementById('mode-tab-upload').click();

    const file = new window.File(['hello audio'], 'audio.mp3', { type: 'audio/mp3' });
    window.selectUploadFile(file);

    let uploadPostCalls = 0;
    window.fetch = vi.fn(async (url, options = {}) => {
      if (typeof url === 'string' && url.endsWith('/api/uploads') && options.method === 'POST') {
        uploadPostCalls += 1;
        // simulate delay
        await new Promise((resolve) => setTimeout(resolve, 50));
        return {
          ok: true,
          status: 202,
          json: async () => ({
            upload_id: 'up-1',
            state: 'accepted',
            task_id: 'task-1',
            view_token: 'upload_view_1',
            retention: '30d',
            expires_at: null,
            share_active: true,
            error_code: null,
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    const form = document.getElementById('transcribe-form');
    // Double click / trigger submit twice
    const p1 = window.submitUploadForm(new window.Event('submit', { cancelable: true }));
    const p2 = window.submitUploadForm(new window.Event('submit', { cancelable: true }));

    await Promise.all([p1, p2]);
    expect(uploadPostCalls).toBe(1);
  });

  it('queries receipt via /api/uploads/by-idempotency-key/{key} on network unknown without resending body', async () => {
    await installIndexPage();
    const { window } = dom;

    const file = new window.File(['bytes'], 'network.mp4', { type: 'video/mp4' });
    window.selectUploadFile(file);

    let postCount = 0;
    let queryKeyCalled = null;

    window.fetch = vi.fn(async (url, options = {}) => {
      if (typeof url === 'string' && url.endsWith('/api/uploads') && options.method === 'POST') {
        postCount += 1;
        throw new TypeError('NetworkError: Failed to fetch');
      }
      if (typeof url === 'string' && url.includes('/api/uploads/by-idempotency-key/')) {
        queryKeyCalled = url.split('/by-idempotency-key/')[1];
        return {
          ok: true,
          status: 200,
          json: async () => ({
            upload_id: 'up-unknown',
            state: 'accepted',
            task_id: 'task-unknown',
            view_token: 'upload_view_unknown',
            retention: '30d',
            expires_at: null,
            share_active: true,
            error_code: null,
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await window.submitUploadForm(new window.Event('submit', { cancelable: true }));

    expect(postCount).toBe(1);
    expect(queryKeyCalled).toBeTruthy();
    expect(dom.window.document.getElementById('status-content').textContent).toContain('任务已受理');
  });

  it('renders upload history item with retention, share badge, and stop share button', async () => {
    dom = new JSDOM(historySource, {
      url: 'https://vta.test/static/history.html',
      runScripts: 'outside-only',
    });
    const { window } = dom;
    window.matchMedia = vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));

    const uploadItem = {
      task_id: 'task-up-1',
      video_url: 'https://example.com/source.mp4',
      request_time: '2026-10-09T12:00:00',
      api_key_masked: 'test-…-token',
      view_token: null,
      upload_view_token: 'active_token_123',
      title: '测试本地视频',
      author: null,
      platform: 'local_upload',
      status: 'success',
      source: 'upload',
      upload_id: 'up-123',
      filename: 'test.mp4',
      source_url: 'https://example.com/source.mp4',
      retention: '30d',
      expires_at: null,
      share_active: true,
      share_inactive_reason: null,
      revoked_at: null,
    };

    window.fetch = vi.fn(async (url, options = {}) => {
      if (typeof url === 'string' && url.includes('/api/audit/history')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            code: 200,
            data: {
              items: [uploadItem],
              total: 1,
              api_key_masked: 'test-…-token',
            },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ code: 200, data: {} }) };
    });

    window.eval(authSource);
    window.VideoTranscriptAuthStorage.writeAuthToken('valid-token', { remember: true });
    const scriptElements = dom.window.document.querySelectorAll('script');
    for (const s of scriptElements) {
      if (!s.src) {
        window.eval(s.textContent);
      }
    }

    await window.loadHistory();

    const listArea = dom.window.document.getElementById('listArea');
    expect(listArea.textContent).toContain('测试本地视频');
    expect(listArea.textContent).toContain('30天保留');
    expect(listArea.textContent).toContain('分享中');

    const stopBtn = listArea.querySelector('.btn-stop-share');
    expect(stopBtn).toBeTruthy();
    expect(stopBtn.getAttribute('data-upload-id')).toBe('up-123');

    const row = listArea.querySelector('.task-row');
    expect(row.getAttribute('data-vt')).toBe('active_token_123');
  });

  it('stops sharing after confirmation and calls DELETE /api/uploads/{id}/share', async () => {
    dom = new JSDOM(historySource, {
      url: 'https://vta.test/static/history.html',
      runScripts: 'outside-only',
    });
    const { window } = dom;
    window.confirm = vi.fn(() => true);

    let deleteCalled = false;
    window.fetch = vi.fn(async (url, options = {}) => {
      if (typeof url === 'string' && url.includes('/api/uploads/up-123/share') && options.method === 'DELETE') {
        deleteCalled = true;
        return {
          ok: true,
          status: 200,
          json: async () => ({ upload_id: 'up-123', share_active: false, revoked_at: '2026-10-09T12:30:00' }),
        };
      }
      if (typeof url === 'string' && url.includes('/api/audit/history')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            code: 200,
            data: {
              items: [],
              total: 0,
              api_key_masked: 'test-…-token',
            },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ code: 200, data: {} }) };
    });

    window.eval(authSource);
    window.VideoTranscriptAuthStorage.writeAuthToken('valid-token', { remember: true });
    const scriptElements = dom.window.document.querySelectorAll('script');
    for (const s of scriptElements) {
      if (!s.src) {
        window.eval(s.textContent);
      }
    }

    await window.loadHistory();
    await window.stopShare('up-123');
    expect(window.confirm).toHaveBeenCalledWith(
      '确认关闭公开分享？关闭后将停止后续阅读、导出与重新处理，但不立即擦除正文，也不取消在途处理与通知。'
    );
    expect(deleteCalled).toBe(true);
  });
});
