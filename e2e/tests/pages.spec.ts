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
  const ossBar = page.locator('.oss-bar');
  await expect(ossBar).toBeVisible();
  await expect(ossBar.locator('a[href="https://github.com/zlxlabs/VideoTranscriptAPI"]')).toBeVisible();
  await expect(ossBar.locator('a[href="https://x.com/bylixing"]')).toBeVisible();

  // Assert oss-bar does not overflow in 375px mobile viewport
  await page.setViewportSize({ width: 375, height: 667 });
  await expect(ossBar).toContainText('张立行');
  const isNotOverflowing = await ossBar.evaluate((el) => el.scrollWidth <= el.clientWidth);
  expect(isNotOverflowing).toBe(true);
  await page.setViewportSize({ width: 1280, height: 720 });

  await expect(page.locator('#calibrated-content-block')).toContainText(
    '隔离浏览器夹具中的正文，验证公开阅读与导出。');
  const exportMenu = page.locator('.export-links-details');
  await exportMenu.locator('summary').click();
  const rawExport = page.getByTitle('导出校对文本（纯文本）');
  await expect(rawExport).toHaveAttribute('href', '/view/browser-fixture-view?raw=calibrated&lang=zh-CN');
  await rawExport.click();
  await expect(page.locator('body')).toContainText('隔离浏览器夹具中的正文，验证公开阅读与导出。');

  await page.goto('/view/browser-fixture-view');
  await page.locator('.export-links-details summary').click();
  const htmlExport = page.getByTitle('导出校对文本（HTML 页面）');
  await expect(htmlExport).toHaveAttribute('href', '/view/browser-fixture-view?page=calibrated&lang=zh-CN');
  await htmlExport.click();
  await expect(page.getByRole('heading', { name: '浏览器回归示例' })).toBeVisible();
  await expect(page.locator('body')).toContainText('隔离浏览器夹具中的正文，验证公开阅读与导出。');
});

test('share page identifies unresolved speaker names without warning of bad separation', async ({ page }) => {
  await page.goto('/view/browser-fixture-view');
  const warnings = page.locator('#calibrated-content-block .speaker-risk-warning');
  await expect(warnings).toHaveCount(1);
  await expect(warnings).toHaveAttribute('data-speaker-risk', 'names_unresolved');
  await expect(warnings).toHaveText('部分说话人姓名未识别');
  await expect(page.getByText('本集说话人区分可能不准', { exact: true })).toHaveCount(0);
});

test('share page warns about potentially inaccurate speaker separation', async ({ page }) => {
  await page.goto('/view/browser-fixture-view?speaker-risk=semantic_contradiction_detected');
  const warnings = page.locator('#calibrated-content-block .speaker-risk-warning');
  await expect(warnings).toHaveCount(1);
  await expect(warnings).toHaveAttribute('data-speaker-risk', 'segmentation');
  await expect(warnings).toHaveText('本集说话人区分可能不准');
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

test('processing view shows current stage and completed stages', async ({ page }) => {
  await page.goto('/view/processing');
  const stageProgress = page.locator('.stage-progress');
  await expect(stageProgress).toBeVisible();
  await expect(stageProgress).toContainText('当前阶段');
  await expect(stageProgress).toContainText('下载');
  await expect(stageProgress).toContainText(/已进行 \d+ 分 \d+ 秒/);
  await expect(stageProgress).toContainText('已完成阶段');
  await expect(stageProgress).toContainText('转录（耗时 1 分 5 秒）');
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

test('share view with card emits twitter large image and og tags', async ({ page }) => {
  const token = 'browser-fixture-view-og';
  const headline = 'Fixture "headline" & alt';
  await page.goto(`/view/${token}`);
  await expect(page.locator('meta[name="twitter:card"]')).toHaveAttribute(
    'content',
    'summary_large_image',
  );
  await expect(page.locator('meta[property="og:image"]')).toHaveAttribute(
    'content',
    `${baseURL}/og/${token}.png?v=1&lang=zh-CN`,
  );
  await expect(page.locator('meta[property="og:image:width"]')).toHaveAttribute(
    'content',
    '1200',
  );
  await expect(page.locator('meta[property="og:image:height"]')).toHaveAttribute(
    'content',
    '630',
  );
  await expect(page.locator('meta[name="twitter:image:alt"]')).toHaveAttribute(
    'content',
    headline,
  );
  await expect(page.locator('meta[property="og:description"]')).toHaveAttribute(
    'content',
    'First fixture point',
  );
});

test('share view without card omits twitter card and og image tags', async ({ page }) => {
  await page.goto('/view/browser-fixture-view');
  await expect(page.locator('meta[name="twitter:card"]')).toHaveCount(0);
  await expect(page.locator('meta[property="og:image"]')).toHaveCount(0);
});
