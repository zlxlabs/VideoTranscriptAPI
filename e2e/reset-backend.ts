import type { APIRequestContext } from '@playwright/test';

export async function resetBackend(request: APIRequestContext, baseURL: string): Promise<void> {
  const response = await request.post(`${baseURL}/__e2e__/reset`);
  if (!response.ok()) {
    throw new Error(`POST /__e2e__/reset failed with HTTP ${response.status()}`);
  }
}
