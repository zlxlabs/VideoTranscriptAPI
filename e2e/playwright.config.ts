import { defineConfig, devices } from '@playwright/test';

const port = process.env.VTA_BROWSER_PORT;
if (!port) throw new Error('VTA_BROWSER_PORT must be assigned by run-browser-tests.mjs');
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.ts',
  outputDir: './node_modules/.cache/playwright-test-results',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL,
    browserName: 'chromium',
    ...devices['Desktop Chrome'],
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `uv run python ../tests/browser_server.py --port ${port}`,
    cwd: process.cwd(),
    url: `${baseURL}/__e2e__/ready`,
    reuseExistingServer: false,
    timeout: 30_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
