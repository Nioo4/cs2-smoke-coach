import { expect, test } from '@playwright/test';

test.describe.configure({ mode: 'serial' });

test('Phase 4 local search preserves local results and exposes a safe no-result state', async ({ page }) => {
  test.skip((process.env.TEST_AI_PROVIDER ?? 'fake') !== 'disabled', 'run after disabled backend recreation');
  test.setTimeout(120_000);

  // The preceding Phase 2 journey created Bench through the visible admin form.
  await page.goto('/admin');
  await page.getByLabel('用户名').fill('admin');
  await page.getByLabel('密码').fill('phase2-local-test-password');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page.getByRole('heading', { name: '把一条路线教清楚。' })).toBeVisible();
  await page.getByRole('tab', { name: '点位' }).click();
  const benchItem = page.locator('.point-directory-item').filter({ hasText: 'bench' });
  await expect(benchItem).toHaveCount(1);
  await page.getByRole('button', { name: '退出' }).click();

  // Exact local requests remain deterministic while Intent is disabled.
  await page.goto('/');
  let input = page.getByRole('textbox', { name: '搜索路线' });
  await input.fill('我在 Mirage 的 T 出生点，想封 VIP');
  await page.getByRole('button', { name: '立即查找' }).click();
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });
  await page.goto('/');
  input = page.getByRole('textbox', { name: '搜索路线' });
  await input.fill('VIP');
  await page.getByRole('button', { name: '立即查找' }).click();
  await expect(page.getByRole('heading', { name: 'VIP · 可用起点' })).toBeVisible({ timeout: 30_000 });

  // No catalog term matches this input, so disabled Intent is reached.
  await page.goto('/');
  input = page.getByRole('textbox', { name: '搜索路线' });
  await input.fill('帮我找一个完全不确定的烟');
  await page.getByRole('button', { name: '立即查找' }).click();
  await expect(page.getByText('意图解析服务暂不可用，请使用明确的目标名称。')).toBeVisible({ timeout: 30_000 });

  // Bench has no maintained local route and therefore stays a local no-result.
  await page.goto('/');
  input = page.getByRole('textbox', { name: '搜索路线' });
  await input.fill('长椅');
  await page.getByRole('button', { name: '立即查找' }).click();
  await expect(page.getByText('暂无符合该位置要求的站内路线，可换个起点或稍后再试。')).toBeVisible({ timeout: 30_000 });

  // A1 -> Bench is active but has no local route, so the result is explicit.
  await page.goto('/');
  input = page.getByRole('textbox', { name: '搜索路线' });
  await input.fill('A1到长椅的烟');
  await page.getByRole('button', { name: '立即查找' }).click();
  await expect(page.getByText('暂无符合该位置要求的站内路线，可换个起点或稍后再试。')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('iframe, video')).toHaveCount(0);

  // Verify the current provider surface through the same visible metrics UI.
  await page.goto('/admin');
  await page.getByLabel('用户名').fill('admin');
  await page.getByLabel('密码').fill('phase2-local-test-password');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page.getByRole('heading', { name: '把一条路线教清楚。' })).toBeVisible();
  await page.getByRole('tab', { name: '指标' }).click();
  await expect(page.getByLabel('Intent 使用明细').locator('dd').first()).toHaveText('0');
  await expect(page.getByLabel('ASR 使用明细')).toHaveCount(1);
});
