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

  // Capture stop share button and token before revoking
  const stopShareBtn = row.locator('.btn-stop-share');
  await expect(stopShareBtn).toBeVisible();
  const token = await row.getAttribute('data-vt');
  expect(token).toBeTruthy();

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
  const viewRes = await request.get(`${baseURL}/view/${token}`);
  expect(viewRes.status()).toBe(404);
});

test('mobile viewport, keyboard selection, and DOM drop submit raw bytes and all-false options', async ({ page, request }) => {
  await page.setViewportSize({ width: 375, height: 667 });
  await page.goto('/add_task_by_web');
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill('browser-fixture-token');
  await page.locator('#mode-tab-upload').click();

  const [fileChooser] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.locator('#upload-drop-zone').press('Enter'),
  ]);
  await fileChooser.setFiles({ name: '初始.mp3', mimeType: 'audio/mp3', buffer: Buffer.from('初始', 'utf-8') });
  await expect(page.locator('#upload-file-name')).toHaveText('初始.mp3');

  const fileBytes = Buffer.from('小屏拖拽上传测试内容', 'utf-8');
  const expectedSha256 = createHash('sha256').update(fileBytes).digest('hex');
  await page.evaluate(({ name, content }) => {
    const dt = new DataTransfer();
    dt.items.add(new File([new Uint8Array(content)], name, { type: 'audio/mp3' }));
    document.getElementById('upload-drop-zone')!.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
  }, { name: '小屏录音.mp3', content: Array.from(fileBytes) });
  await expect(page.locator('#upload-file-name')).toHaveText('小屏录音.mp3');

  await page.locator('#transcription-options-toggle').click();
  await page.locator('#calibrate-option').uncheck();
  await page.locator('#summarize-option').uncheck();

  await page.locator('#upload-title').fill('移动端小屏标题');
  await page.locator('#submit-btn').click();
  await expect(page.locator('#status-content')).toContainText('文件上传成功，任务已受理！');

  const lastUploadRes = await request.get(`${baseURL}/__e2e__/last-upload`);
  expect(lastUploadRes.status()).toBe(200);
  const lastUpload = await lastUploadRes.json();
  expect(lastUpload.actual_byte_length).toBe(fileBytes.length);
  expect(lastUpload.raw_bytes_sha256).toBe(expectedSha256);
  expect(lastUpload.filename).toBe('小屏录音.mp3');
  expect(lastUpload.title).toBe('移动端小屏标题');
  expect(lastUpload.processing_options).toEqual({
    calibrate: false,
    summarize: false,
    infer_speaker_names: false,
    chapters: false,
  });
});

test('post 500 followed by get 404, receiving, and failed preserves key and prevents second post', async ({ page }) => {
  await page.goto('/add_task_by_web');
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill('browser-fixture-token');
  await page.locator('#mode-tab-upload').click();

  let postCount = 0;
  let getCount = 0;
  let postKey = '';
  let getResponseState = '404';

  await page.route('**/api/uploads', async (route) => {
    if (route.request().method() === 'POST') {
      postCount++;
      postKey = route.request().headers()['idempotency-key'];
      await route.fulfill({ status: 500, body: JSON.stringify({ detail: 'server error' }) });
    } else {
      await route.continue();
    }
  });

  await page.route('**/api/uploads/by-idempotency-key/*', async (route) => {
    getCount++;
    const key = route.request().url().split('/by-idempotency-key/')[1];
    expect(decodeURIComponent(key)).toBe(postKey);
    if (getResponseState === '404') {
      await route.fulfill({ status: 404, body: JSON.stringify({ detail: 'not found' }) });
    } else {
      await route.fulfill({
        status: 200,
        body: JSON.stringify({
          upload_id: 'up-123',
          state: getResponseState,
          task_id: null,
          error_code: getResponseState === 'failed' ? 'transcode_err' : null,
        }),
      });
    }
  });

  await page.locator('#upload-file-input').setInputFiles({
    name: '待核实.mp3',
    mimeType: 'audio/mp3',
    buffer: Buffer.from('500内容', 'utf-8'),
  });

  // 1st click: POST 500 -> initial GET 404 -> status pending
  await page.locator('#submit-btn').click();
  await expect(page.locator('#status-content')).toContainText('上传状态待核实');
  expect(postCount).toBe(1);
  expect(getCount).toBe(1);

  // 2nd click: GET only with same key -> returns receiving, no local history
  getResponseState = 'receiving';
  await page.locator('#submit-btn').click();
  await expect(page.locator('#status-content')).toContainText('文件已接收，等待排队受理');
  await expect(page.locator('#status-content')).not.toContainText('文件上传成功，任务已受理！');
  await expect(page.locator('#history-list .history-item')).toHaveCount(0);
  expect(postCount).toBe(1);
  expect(getCount).toBe(2);

  // 3rd click: GET only with same key -> returns failed, no local history
  getResponseState = 'failed';
  await page.locator('#submit-btn').click();
  await expect(page.locator('#status-content')).toContainText('上传处理失败');
  await expect(page.locator('#status-content')).toContainText('transcode_err');
  await expect(page.locator('#history-list .history-item')).toHaveCount(0);
  expect(postCount).toBe(1);
  expect(getCount).toBe(3);

  browserErrors.get(page)?.splice(0);
});

test('parameter change with colon variations produces distinct idempotency keys', async ({ page }) => {
  await page.goto('/add_task_by_web');
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill('browser-fixture-token');
  await page.locator('#mode-tab-upload').click();

  const keys: string[] = [];
  const metadataList: any[] = [];
  await page.route('**/api/uploads', async (route) => {
    if (route.request().method() === 'POST') {
      keys.push(route.request().headers()['idempotency-key']);
      const raw = route.request().headers()['x-upload-metadata'];
      const padded = raw + '='.repeat((4 - (raw.length % 4)) % 4);
      metadataList.push(JSON.parse(Buffer.from(padded, 'base64url').toString('utf-8')));
      await route.fulfill({ status: 400, body: JSON.stringify({ detail: 'rejected' }) });
    } else {
      await route.continue();
    }
  });

  await page.locator('#upload-file-input').setInputFiles({
    name: '冒号测试.mp3',
    mimeType: 'audio/mp3',
    buffer: Buffer.from('冒号测试', 'utf-8'),
  });

  await page.locator('#upload-title').fill('T');
  await page.locator('#upload-source-url').fill('https://example.com/a:b');
  await page.locator('#submit-btn').click();
  await expect(page.locator('#status-content')).toContainText('rejected');

  await page.locator('#upload-title').fill('T:b');
  await page.locator('#upload-source-url').fill('https://example.com/a');
  await page.locator('#submit-btn').click();
  await expect(page.locator('#status-content')).toContainText('rejected');

  expect(keys.length).toBe(2);
  expect(keys[0]).not.toBe(keys[1]);
  expect(metadataList[0].title).toBe('T');
  expect(metadataList[0].source_url).toBe('https://example.com/a:b');
  expect(metadataList[1].title).toBe('T:b');
  expect(metadataList[1].source_url).toBe('https://example.com/a');

  browserErrors.get(page)?.splice(0);
});

test('upload in flight: selecting replacement file binds original snapshot to upload and preserves replacement file', async ({ page, request }) => {
  await page.goto('/add_task_by_web');
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill('browser-fixture-token');
  await page.locator('#mode-tab-upload').click();

  const file1Bytes = Buffer.from('第一份音频内容-原意图', 'utf-8');
  const file1Sha256 = createHash('sha256').update(file1Bytes).digest('hex');

  await page.locator('#upload-file-input').setInputFiles({
    name: '原文件.mp3',
    mimeType: 'audio/mp3',
    buffer: file1Bytes,
  });

  // Pause the POST request via page.route
  let continueRoute: () => void = () => {};
  const routePromise = new Promise<void>((resolve) => {
    continueRoute = resolve;
  });

  await page.route('**/api/uploads', async (route) => {
    if (route.request().method() === 'POST') {
      await routePromise;
      await route.continue();
    } else {
      await route.continue();
    }
  });

  // Trigger submit
  await page.locator('#submit-btn').click();

  // While in flight, select a second file
  const file2Bytes = Buffer.from('第二份音频内容-新文件', 'utf-8');
  await page.locator('#upload-file-input').setInputFiles({
    name: '替换新文件.mp4',
    mimeType: 'video/mp4',
    buffer: file2Bytes,
  });

  // Now resume the original POST
  continueRoute();

  // Wait for submission to complete
  await expect(page.locator('#status-content')).toContainText('文件上传成功，任务已受理！');

  // Assert backend received file1 snapshot
  const lastUploadRes = await request.get(`${baseURL}/__e2e__/last-upload`);
  expect(lastUploadRes.status()).toBe(200);
  const lastUpload = await lastUploadRes.json();
  expect(lastUpload.filename).toBe('原文件.mp3');
  expect(lastUpload.raw_bytes_sha256).toBe(file1Sha256);

  // Assert replacement file is still selected and visible in UI
  const fileInfo = page.locator('#upload-file-info');
  await expect(fileInfo).toBeVisible();
  await expect(page.locator('#upload-file-name')).toHaveText('替换新文件.mp4');

  // Assert local task history has file1 title
  const historyItem = page.locator('#history-list .history-item').first();
  await expect(historyItem).toBeVisible();
  await expect(historyItem.locator('.history-title')).toHaveText('原文件.mp3');
});

