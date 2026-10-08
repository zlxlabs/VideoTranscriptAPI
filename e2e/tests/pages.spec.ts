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
  await resetBackend(request, baseURL);
});

test.afterEach(async ({ page }) => {
  expect(browserErrors.get(page) ?? []).toEqual([]);
});

test('submission page sends selected URL and shows the accepted task', async ({ page }) => {
  await page.goto('/add_task_by_web');
  await expect(page.getByRole('heading', { name: '视频转录服务' })).toBeVisible();
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill('browser-fixture-token');
  await page.locator('#share-content').fill('https://www.youtube.com/watch?v=browser-fixture');
  await expect(page.locator('.url-option').first()).toBeVisible();

  const submitted = page.waitForRequest((request) =>
    request.url().endsWith('/api/transcribe') && request.method() === 'POST');
  await page.locator('#submit-btn').click();
  const request = await submitted;
  expect(request.postDataJSON()).toEqual({
    url: 'https://www.youtube.com/watch?v=browser-fixture',
    use_speaker_recognition: false,
  });
  await expect(page.locator('#status-content')).toContainText('任务提交成功！');
  await expect(page.locator('#status-content a[href="/view/browser-fixture-view"]')).toBeVisible();
});

test('history query renders a seeded task and opening it marks it read', async ({ page }) => {
  await page.goto('/static/history.html');
  await page.locator('#apiKeyInput').fill('browser-fixture-token');
  await page.locator('#historyQueryBtn').click();
  const row = page.locator('.task-row[data-vt="browser-fixture-view"]');
  await expect(row.locator('.task-title')).toHaveText('浏览器回归历史样本');
  await expect(row.locator('.read-dot')).toHaveCount(1);

  const popupPromise = page.waitForEvent('popup');
  await row.click();
  const popup = await popupPromise;
  await expect(popup.locator('h1')).toHaveText('浏览器回归示例');
  await expect(row).toHaveClass(/read/);
  await expect(row.locator('.read-dot')).toHaveCount(0);
});

test('public reading page displays transcript and opens a working text export', async ({ page }) => {
  await page.goto('/view/browser-fixture-view');
  await expect(page.locator('#calibrated-content-block')).toContainText(
    '隔离浏览器夹具中的正文，验证公开阅读与导出。');
  const exportMenu = page.locator('.export-links-details');
  await exportMenu.locator('summary').click();
  const rawExport = page.getByTitle('导出校对文本（纯文本）');
  await expect(rawExport).toHaveAttribute('href', '/view/browser-fixture-view?raw=calibrated');
  await rawExport.click();
  await expect(page.locator('body')).toContainText('隔离浏览器夹具中的正文，验证公开阅读与导出。');
});

test('home page smoke: production landing content and entry links render', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'VideoTranscriptAPI' })).toBeVisible();
  await expect(page.getByRole('link', { name: /提交任务/ })).toHaveAttribute('href', '/add_task_by_web');
  await expect(page.getByRole('link', { name: /任务历史/ })).toHaveAttribute('href', '/static/history.html');
});

test('submission page smoke: actual form and navigation are available', async ({ page }) => {
  await page.goto('/add_task_by_web');
  await expect(page.locator('#transcribe-form')).toBeVisible();
  await expect(page.locator('#share-content')).toBeVisible();
  await expect(page.locator('#submit-btn')).toBeDisabled();
});

test('history page smoke: authentication and task list controls render', async ({ page }) => {
  await page.goto('/static/history.html');
  await expect(page.getByRole('heading', { name: '任务历史' })).toBeVisible();
  await expect(page.locator('#apiKeyInput')).toBeVisible();
  await expect(page.locator('#historyQueryBtn')).toBeVisible();
});

test('processing view smoke: processing state is visible', async ({ page }) => {
  await page.goto('/view/processing');
  await expect(page.getByRole('heading', { name: /转录处理中/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /刷新页面/ })).toBeVisible();
});

test('failed view smoke: error state is visible', async ({ page }) => {
  await page.goto('/view/failed');
  await expect(page.getByRole('heading', { name: /出现错误/ })).toBeVisible();
  await expect(page.getByText('夹具失败状态')).toBeVisible();
});

test('cleaned view smoke: cleanup state and recovery guidance are visible', async ({ page }) => {
  await page.goto('/view/cleaned');
  await expect(page.getByRole('heading', { name: /内容已清理/ })).toBeVisible();
  await expect(page.getByText(/重新获取转录/)).toBeVisible();
});
