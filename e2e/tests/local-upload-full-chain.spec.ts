import { createHash } from 'node:crypto';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const REPO_ROOT = resolve(process.cwd(), '..');
const OWNER_TOKEN = 'browser-fixture-token';
const ASR_TEXT = '这段转录由真实 CapsWriter SDK 从浏览器上传的 WAV 文件经 ffmpeg 转码后生成。本地上传会写入独立 SQLite 记录，经过实际任务队列处理，并通过公开只读链接提供正文。这个受控 loopback 服务只替代外部识别、模型和通知接收端，产品路由与序列化仍真实运行。';
const LLM_TEXT = `${ASR_TEXT}共享模型客户端已读取完整转录正文并完成校对；这段追加说明用于确认真实 API、SDK 输出与模型 HTTP 请求之间的内容链路完整。`;

type RealServer = { api_url: string; upstream_url: string; run_dir: string };

let realServer: RealServer;
let serverProcess: ChildProcessWithoutNullStreams;
let serverOutput = '';
let serverScratch: string;

test.describe.configure({ timeout: 180_000 });

function makeWav(): Buffer {
  const sampleRate = 16_000;
  const durationSeconds = 3;
  const sampleCount = sampleRate * durationSeconds;
  const bytes = Buffer.alloc(44 + sampleCount * 2);
  bytes.write('RIFF', 0);
  bytes.writeUInt32LE(bytes.length - 8, 4);
  bytes.write('WAVE', 8);
  bytes.write('fmt ', 12);
  bytes.writeUInt32LE(16, 16);
  bytes.writeUInt16LE(1, 20);
  bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(sampleRate, 24);
  bytes.writeUInt32LE(sampleRate * 2, 28);
  bytes.writeUInt16LE(2, 32);
  bytes.writeUInt16LE(16, 34);
  bytes.write('data', 36);
  bytes.writeUInt32LE(sampleCount * 2, 40);
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = Math.round(Math.sin((2 * Math.PI * 440 * index) / sampleRate) * 8000);
    bytes.writeInt16LE(sample, 44 + index * 2);
  }
  return bytes;
}

function startRealServer(): Promise<RealServer> {
  return new Promise((resolveReady, rejectReady) => {
    const python = join(REPO_ROOT, '.venv', 'bin', 'python');
    const spawnEnv = {
      PATH: process.env.PATH ?? '/usr/bin:/bin',
      HOME: process.env.HOME ?? '/tmp',
      TMPDIR: '/tmp',
      LANG: 'C.UTF-8',
      PYTHONUTF8: '1',
      PYTHONUNBUFFERED: '1',
    };
    let lineBuffer = '';
    let settled = false;
    const startupTimer = setTimeout(() => {
      if (settled) return;
      settled = true;
      rejectReady(new Error(`Real API did not become ready. Output:\n${serverOutput}`));
    }, 45_000);

    serverProcess = spawn(
      python,
      ['tests/local_upload_real_server.py', '--run-dir', serverScratch],
      { cwd: REPO_ROOT, env: spawnEnv, stdio: ['ignore', 'pipe', 'pipe'] },
    );
    serverProcess.stdout.on('data', (chunk: Buffer) => {
      const text = chunk.toString('utf-8');
      serverOutput += text;
      lineBuffer += text;
      const lines = lineBuffer.split('\n');
      lineBuffer = lines.pop() ?? '';
      for (const line of lines) {
        const marker = 'VTA_REAL_SERVER_READY ';
        const index = line.indexOf(marker);
        if (index < 0 || settled) continue;
        try {
          const ready = JSON.parse(line.slice(index + marker.length)) as RealServer;
          settled = true;
          clearTimeout(startupTimer);
          resolveReady(ready);
        } catch (error) {
          settled = true;
          clearTimeout(startupTimer);
          rejectReady(error);
        }
      }
    });
    serverProcess.stderr.on('data', (chunk: Buffer) => {
      serverOutput += chunk.toString('utf-8');
    });
    serverProcess.once('error', (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(startupTimer);
      rejectReady(error);
    });
    serverProcess.once('exit', (code, signal) => {
      if (settled) return;
      settled = true;
      clearTimeout(startupTimer);
      rejectReady(new Error(`Real API exited before ready: code=${code} signal=${signal}\n${serverOutput}`));
    });
  });
}

async function jsonResponse<T>(request: APIRequestContext, url: string, headers?: Record<string, string>): Promise<T> {
  const response = await request.get(url, { headers });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()) as T;
}

async function loopbackEvents(request: APIRequestContext): Promise<any> {
  return jsonResponse(request, `${realServer.upstream_url}/__e2e__/events`);
}

async function inspectRealState(request: APIRequestContext): Promise<any> {
  return jsonResponse(request, `${realServer.api_url}/__e2e__/state`);
}

async function selectUploadPage(page: Page): Promise<void> {
  await page.goto(`${realServer.api_url}/add_task_by_web`);
  await page.locator('#advanced-toggle').click();
  await page.locator('#bearer-token').fill(OWNER_TOKEN);
  await page.locator('#mode-tab-upload').click();
  await expect(page.locator('#upload-mode-panel')).toBeVisible();
}

test.beforeAll(async () => {
  serverScratch = await mkdtemp(join(tmpdir(), 'vta-upload-real-chain-'));
  realServer = await startRealServer();
  const ready = await fetch(`${realServer.api_url}/livez`);
  expect(ready.status).toBe(200);
});

test.afterAll(async () => {
  if (realServer) {
    const release = await fetch(`${realServer.upstream_url}/__e2e__/release-asr`, { method: 'POST' });
    expect(release.status).toBe(200);
    await expect.poll(async () => {
      const response = await fetch(`${realServer.api_url}/__e2e__/state`);
      if (response.status !== 200) return `http-${response.status}`;
      const state = await response.json();
      if (!state.uploads.length) return 'idle';
      return state.uploads[0].root_status;
    }, { timeout: 60_000 }).toMatch(/^(idle|success|failed)$/);
  }
  if (serverProcess?.exitCode === null && serverProcess?.signalCode === null) {
    await new Promise<void>((resolveExit, rejectExit) => {
      const timeout = setTimeout(() => {
        rejectExit(new Error(`Real API did not stop cleanly. Output:\n${serverOutput}`));
      }, 20_000);
      serverProcess.once('exit', () => {
        clearTimeout(timeout);
        resolveExit();
      });
      serverProcess.kill('SIGTERM');
    });
  }
  if (serverScratch) await rm(serverScratch, { recursive: true, force: true });
});

test('Chromium uploads raw media through production API, worker, shared ASR/LLM, history, read, notification, and owner revoke', async ({ page, request }, testInfo) => {
  const browserErrors: string[] = [];
  page.on('pageerror', (error) => browserErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') browserErrors.push(message.text());
  });

  const wavBytes = makeWav();
  const expectedDigest = createHash('sha256').update(wavBytes).digest('hex');
  await page.addInitScript(() => {
    const originalFetch = window.fetch;
    (window as any).__vtaUploadProducerBytes = null;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.endsWith('/api/uploads') && init?.method === 'POST' && init.body instanceof Blob) {
        (window as any).__vtaUploadProducerBytes = Array.from(
          new Uint8Array(await init.body.arrayBuffer()),
        );
      }
      return originalFetch.call(window, input, init);
    };
  });
  await selectUploadPage(page);
  await page.locator('#upload-title').fill('全链路浏览器上传样本');
  await page.locator('#upload-source-url').fill('https://example.test/source/interview');
  await page.locator('#transcription-options-toggle').click();
  await page.locator('#summarize-option').uncheck();
  await page.locator('#upload-file-input').setInputFiles({
    name: '浏览器全链路音频.wav',
    mimeType: 'audio/wav',
    buffer: wavBytes,
  });

  const uploadRequestPromise = page.waitForRequest((item) =>
    item.method() === 'POST' && new URL(item.url()).pathname === '/api/uploads');
  const uploadResponsePromise = page.waitForResponse((item) =>
    item.request().method() === 'POST' && new URL(item.url()).pathname === '/api/uploads');
  await page.locator('#submit-btn').click();
  const [uploadRequest, uploadResponse] = await Promise.all([uploadRequestPromise, uploadResponsePromise]);
  expect(uploadResponse.status(), await uploadResponse.text()).toBe(202);
  const receipt = await uploadResponse.json();
  const requestHeaders = uploadRequest.headers();
  const producerBytes = Buffer.from(await page.evaluate(
    () => (window as any).__vtaUploadProducerBytes as number[],
  ));
  expect(producerBytes).toEqual(wavBytes);
  await writeFile(testInfo.outputPath('browser-producer-upload.wav'), producerBytes);
  expect(requestHeaders.authorization).toBe(`Bearer ${OWNER_TOKEN}`);
  expect(requestHeaders['content-type']).toBe('application/octet-stream');
  expect(requestHeaders['idempotency-key']).toMatch(/^\d+-[0-9a-f-]+$/i);
  const metadata = JSON.parse(Buffer.from(requestHeaders['x-upload-metadata'], 'base64url').toString('utf-8'));
  expect(metadata).toEqual({
    filename: '浏览器全链路音频.wav',
    byte_size: wavBytes.length,
    title: '全链路浏览器上传样本',
    source_url: 'https://example.test/source/interview',
    retention: '30d',
    processing_options: {
      calibrate: true,
      summarize: false,
      infer_speaker_names: false,
      chapters: false,
    },
  });
  await writeFile(testInfo.outputPath('browser-producer-metadata.json'), JSON.stringify(metadata, null, 2));
  expect(receipt.state).toBe('accepted');
  expect(receipt.upload_id).toBeTruthy();
  expect(receipt.task_id).toBeTruthy();
  expect(receipt.view_token).toMatch(/^upload_/);
  await expect(page.locator('#status-content')).toContainText('文件上传成功，任务已受理！');

  await expect.poll(async () => (await loopbackEvents(request)).asr_started, { timeout: 20_000 }).toBe(true);
  const beforeAsrReply = await inspectRealState(request);
  expect(beforeAsrReply.uploads).toHaveLength(1);
  const accepted = beforeAsrReply.uploads[0];
  expect(accepted.upload_id).toBe(receipt.upload_id);
  expect(accepted.owner_user_id).toBe('browser_owner');
  expect(accepted.idempotency_key).toBe(requestHeaders['idempotency-key']);
  expect(accepted.state).toBe('accepted');
  expect(accepted.root_task_id).toBe(receipt.task_id);
  expect(accepted.view_token).toBe(receipt.view_token);
  expect(accepted.root_status).toBe('processing');
  expect(accepted.task_platform).toBe('local_upload');
  expect(accepted.legacy_task_token).toBe('');
  expect(accepted.media_exists).toBe(true);
  expect(accepted.media_path).toContain('/upload-source.bin');
  expect(accepted.media_path).not.toContain(accepted.filename);
  expect(accepted.byte_size).toBe(wavBytes.length);
  expect(accepted.sha256).toBe(expectedDigest);
  expect(accepted.media_sha256).toBe(expectedDigest);
  const storedBytes = await readFile(accepted.media_path);
  expect(storedBytes).toEqual(producerBytes);
  const ffprobeCall = beforeAsrReply.ffprobe_calls.find((call: any) => call.argv.at(-1) === accepted.media_path);
  expect(ffprobeCall).toBeTruthy();
  expect(ffprobeCall.argv).toContain('-show_format');
  expect(ffprobeCall.argv).toContain('-show_streams');
  expect(ffprobeCall.argv).toContain('-v');
  await writeFile(testInfo.outputPath('real-ffprobe-argv.json'), JSON.stringify(ffprobeCall, null, 2));

  const release = await request.post(`${realServer.upstream_url}/__e2e__/release-asr`);
  expect(release.status()).toBe(200);
  await expect.poll(async () => {
    const state = await inspectRealState(request);
    return state.uploads[0].root_status;
  }, { timeout: 60_000 }).toBe('success');

  const finished = await inspectRealState(request);
  const completed = finished.uploads[0];
  expect(completed.root_status).toBe('success');
  expect(completed.expires_at).toBeTruthy();
  expect(completed.legacy_task_token).toBe('');
  expect(completed.media_exists).toBe(false);
  expect(completed.sha256).toBe(expectedDigest);

  const events = await loopbackEvents(request);
  expect(events.asr_payload_bytes).toBeGreaterThan(0);
  expect(events.asr_reply).toMatchObject({
    task_id: expect.any(String),
    text_accu: ASR_TEXT,
    source: 'file',
    encoding: 'flac',
    samples_total: expect.any(Number),
  });
  const sdkFrames = events.asr_frames.map((raw: string) => JSON.parse(raw));
  expect(sdkFrames).toHaveLength(1);
  expect(sdkFrames[0]).toMatchObject({
    task_id: events.asr_reply.task_id,
    source: 'file',
    encoding: 'flac',
    is_final: true,
  });
  expect(Buffer.from(sdkFrames[0].data, 'base64').length).toBeGreaterThan(0);
  await writeFile(testInfo.outputPath('capswriter-sdk-frame.json'), events.asr_frames[0]);
  expect(events.llm_requests.length).toBeGreaterThan(0);
  expect(events.llm_requests.some((entry: any) =>
    JSON.stringify(entry.messages).includes(ASR_TEXT))).toBe(true);

  const authHeaders = { Authorization: `Bearer ${OWNER_TOKEN}` };
  const history = await jsonResponse<any>(
    request,
    `${realServer.api_url}/api/audit/history?source=upload&status=all&limit=1&offset=0`,
    authHeaders,
  );
  expect(history.data.total).toBe(1);
  expect(history.data.items[0]).toMatchObject({
    source: 'upload',
    upload_id: receipt.upload_id,
    title: '全链路浏览器上传样本',
    share_active: true,
    view_token: null,
  });

  const publicRaw = await request.get(`${realServer.api_url}/view/${receipt.view_token}?raw=transcript`);
  expect(publicRaw.status()).toBe(200);
  const rawBody = await publicRaw.text();
  expect(rawBody).toContain(ASR_TEXT);
  const publicCalibrated = await request.get(`${realServer.api_url}/view/${receipt.view_token}?raw=calibrated`);
  expect(publicCalibrated.status()).toBe(200);
  const calibratedBody = await publicCalibrated.text();
  expect(calibratedBody).toContain(LLM_TEXT);
  await page.goto(`${realServer.api_url}/view/${receipt.view_token}`);
  await expect(page.locator('body')).toContainText('全链路浏览器上传样本');
  await expect(page.locator('body')).toContainText(LLM_TEXT);

  await expect.poll(async () => {
    const notifications = (await loopbackEvents(request)).notifications;
    return notifications.some((entry: any) => JSON.stringify(entry.payload).includes(`/view/${receipt.view_token}`));
  }, { timeout: 20_000 }).toBe(true);
  const notification = (await loopbackEvents(request)).notifications.find((entry: any) =>
    JSON.stringify(entry.payload).includes(`/view/${receipt.view_token}`));
  expect(notification.content_type).toContain('application/json');
  expect(notification.payload.msg_type).toBe('interactive');
  expect(notification.raw_sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(JSON.stringify(notification.payload)).toContain('全链路浏览器上传样本');
  expect(JSON.stringify(notification.payload)).not.toContain('upload-source.bin');
  await writeFile(testInfo.outputPath('actual-notification-http-json.json'), JSON.stringify(notification.payload, null, 2));

  await page.goto(`${realServer.api_url}/static/history.html`);
  await page.locator('#apiKeyInput').fill(OWNER_TOKEN);
  await page.locator('#historyQueryBtn').click();
  await page.locator('#filterSource').selectOption('upload');
  const row = page.locator('.task-row').first();
  await expect(row.locator('.task-title')).toContainText('全链路浏览器上传样本');
  const stopShare = row.locator('.btn-stop-share');
  await expect(stopShare).toBeVisible();
  page.once('dialog', async (dialog) => dialog.accept());
  const deleteRequest = page.waitForRequest((item) =>
    item.method() === 'DELETE' && new URL(item.url()).pathname === `/api/uploads/${receipt.upload_id}/share`);
  const deleteResponse = page.waitForResponse((item) =>
    item.request().method() === 'DELETE' && new URL(item.url()).pathname === `/api/uploads/${receipt.upload_id}/share`);
  await stopShare.click();
  const [ownerDelete, deleteResult] = await Promise.all([deleteRequest, deleteResponse]);
  expect(ownerDelete.headers().authorization).toBe(`Bearer ${OWNER_TOKEN}`);
  expect(deleteResult.status()).toBe(200);
  expect((await deleteResult.json()).share_active).toBe(false);
  await expect(page.locator('#authStatus')).toContainText('已停止公开分享');

  const deniedFreshRead = await request.get(`${realServer.api_url}/view/${receipt.view_token}?raw=transcript`);
  expect(deniedFreshRead.status()).toBe(404);
  expect(await deniedFreshRead.text()).not.toContain(ASR_TEXT);
  const revoked = (await inspectRealState(request)).uploads[0];
  expect(revoked.revoked_at).toBeTruthy();
  expect(revoked.root_status).toBe('success');
  expect(browserErrors).toEqual([]);
});
