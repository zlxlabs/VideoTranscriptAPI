import { createHash } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';
import { resetBackend } from '../reset-backend';

const baseURL = process.env.VTA_BROWSER_PORT
  ? `http://127.0.0.1:${process.env.VTA_BROWSER_PORT}`
  : '';
const browserErrors = new WeakMap<Page, string[]>();

function observeBrowser(page: Page): void {
  const errors: string[] = [];
  browserErrors.set(page, errors);
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('requestfailed', (request) => {
    errors.push(`request failed: ${request.method()} ${request.url()} (${request.failure()?.errorText})`);
  });
  page.on('response', (response) => {
    if (response.status() >= 400) {
      errors.push(`HTTP ${response.status()}: ${response.request().method()} ${response.url()}`);
    }
  });
}

test.beforeEach(async ({ page, request }) => {
  if (!baseURL) throw new Error('VTA_BROWSER_PORT must be set by the browser test runner');
  observeBrowser(page);
  page.context().on('page', observeBrowser);
  await resetBackend(request, baseURL);
});

test.afterEach(async ({ page }) => {
  for (const observedPage of page.context().pages()) {
    expect(browserErrors.get(observedPage) ?? []).toEqual([]);
  }
});

test('upload page selects local media, submits raw bytes with metadata headers, and captures server receipt', async ({ page, request }) => {
  await page.goto('/add_task_by_web');
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill('browser-fixture-token');

  // Switch to upload tab
  await page.locator('#mode-tab-upload').click();
  await expect(page.locator('#upload-mode-panel')).toBeVisible();
  await expect(page.locator('#url-mode-panel')).toBeHidden();

  // Fill in optional title and source url
  await page.locator('#upload-title').fill('浏览器测试上传标题');
  await page.locator('#upload-source-url').fill('https://example.com/source-podcast');

  // Set file payload
  const fileBytes = Buffer.from('真实浏览器音视频字节内容-SHA验证', 'utf-8');
  const expectedSha256 = createHash('sha256').update(fileBytes).digest('hex');

  const fileInput = page.locator('#upload-file-input');
  await fileInput.setInputFiles({
    name: '测试音频.mp3',
    mimeType: 'audio/mp3',
    buffer: fileBytes,
  });

  // Verify file info is displayed
  await expect(page.locator('#upload-file-info')).toBeVisible();
  await expect(page.locator('#upload-file-name')).toHaveText('测试音频.mp3');
  await expect(page.locator('#submit-btn')).toBeEnabled();

  // Intercept actual HTTP POST request from browser
  const submitted = page.waitForRequest((req) =>
    req.url().endsWith('/api/uploads') && req.method() === 'POST');

  await page.locator('#submit-btn').click();
  const req = await submitted;

  // Assert headers
  const headers = req.headers();
  expect(headers['authorization']).toBe('Bearer browser-fixture-token');
  expect(headers['content-type']).toBe('application/octet-stream');
  expect(headers['idempotency-key']).toMatch(/^\d+-[0-9a-fA-F-]+$/);

  const rawMetadata = headers['x-upload-metadata'];
  expect(rawMetadata).toBeTruthy();
  const padded = rawMetadata + '='.repeat((4 - (rawMetadata.length % 4)) % 4);
  const metadataJson = JSON.parse(Buffer.from(padded, 'base64url').toString('utf-8'));

  expect(metadataJson).toEqual({
    filename: '测试音频.mp3',
    byte_size: fileBytes.length,
    title: '浏览器测试上传标题',
    source_url: 'https://example.com/source-podcast',
    retention: '30d',
    processing_options: {
      calibrate: true,
      summarize: true,
      infer_speaker_names: false,
      chapters: true,
    },
  });

  // Assert actual raw body bytes captured by browser server fixture
  const lastUploadRes = await request.get(`${baseURL}/__e2e__/last-upload`);
  expect(lastUploadRes.status()).toBe(200);
  const lastUpload = await lastUploadRes.json();
  expect(lastUpload.actual_byte_length).toBe(fileBytes.length);
  expect(lastUpload.raw_bytes_sha256).toBe(expectedSha256);
  expect(lastUpload.filename).toBe('测试音频.mp3');
  expect(lastUpload.byte_size).toBe(fileBytes.length);

  // Assert UI feedback shows accepted receipt
  await expect(page.locator('#status-content')).toContainText('文件上传成功，任务已受理！');
  await expect(page.locator('#status-content')).toContainText('上传ID:');
  await expect(page.locator('#status-content a[href^="/view/"]')).toBeVisible();
});

test('upload page prevents double submission on duplicate clicks', async ({ page }) => {
  await page.goto('/add_task_by_web');
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill('browser-fixture-token');

  await page.locator('#mode-tab-upload').click();

  const fileBytes = Buffer.from('单次意图防重测试', 'utf-8');
  await page.locator('#upload-file-input').setInputFiles({
    name: '防重测试.mp3',
    mimeType: 'audio/mp3',
    buffer: fileBytes,
  });

  let postCount = 0;
  page.on('request', (req) => {
    if (req.url().endsWith('/api/uploads') && req.method() === 'POST') {
      postCount += 1;
    }
  });

  const submitBtn = page.locator('#submit-btn');
  // Rapid double click
  await Promise.all([
    submitBtn.click({ clickCount: 2 }),
  ]);

  await expect(page.locator('#status-content')).toContainText('文件上传成功，任务已受理！');
  expect(postCount).toBe(1);
});

test('history page filters upload tasks, displays retention and share state, and allows owner to stop sharing', async ({ page, request }) => {
  // First upload a file to have an active upload record
  await page.goto('/add_task_by_web');
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill('browser-fixture-token');
  await page.locator('#mode-tab-upload').click();
  await page.locator('#upload-title').fill('本人上传历史项目');

  const fileBytes = Buffer.from('本人历史与停止分享测试', 'utf-8');
  await page.locator('#upload-file-input').setInputFiles({
    name: '本人分享.mp4',
    mimeType: 'video/mp4',
    buffer: fileBytes,
  });
  await page.locator('#submit-btn').click();
  await expect(page.locator('#status-content')).toContainText('文件上传成功，任务已受理！');

  // Now open history page
  await page.goto('/static/history.html');
  await page.locator('#apiKeyInput').fill('browser-fixture-token');
  await page.locator('#historyQueryBtn').click();

  // Wait for initial load to finish before changing filter to avoid aborting in-flight requests
  await expect(page.locator('.task-row[data-vt="browser-fixture-view"]')).toBeVisible();

  // Switch source filter to local upload
  await page.locator('#filterSource').selectOption('upload');

  // Wait for list to render upload item
  const row = page.locator('.task-row').first();
  await expect(row.locator('.task-title')).toContainText('本人上传历史项目');
  await expect(row.locator('.task-author')).toContainText('30天保留');
  await expect(row.locator('.task-author')).toContainText('分享中');

  // Verify stop share button is visible
  const stopShareBtn = row.locator('.btn-stop-share');
  await expect(stopShareBtn).toBeVisible();

  // Set up dialog confirmation
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('确认关闭公开分享？关闭后将停止后续阅读、导出与重新处理');
    await dialog.accept();
  });

  // Click stop share button
  await stopShareBtn.click();

  // Assert status and updated UI
  await expect(page.locator('#authStatus')).toContainText('已停止公开分享');
  await expect(row.locator('.task-author')).toContainText('已停止分享');
  await expect(row.locator('.btn-stop-share')).toHaveCount(0);

  // Assert backend revoked state directly via API
  const token = await row.getAttribute('data-vt');
  if (token) {
    const viewRes = await request.get(`${baseURL}/view/${token}`);
    expect(viewRes.status()).toBe(404);
  }
});
