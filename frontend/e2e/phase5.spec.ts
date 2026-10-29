import { expect, test } from '@playwright/test';

test('E2E-10 录音转写回填并自动检索', async ({ page }) => {
  test.setTimeout(60_000);
  // Chromium exposes getUserMedia only in secure contexts. The isolated
  // nginx service is HTTP, so use the localhost secure-context exception and
  // map it to nginx in the Playwright-only browser process.
  await page.goto('http://localhost:8080/');
  const input = page.getByRole('textbox', { name: '搜索路线' });
  const microphone = page.getByRole('button', { name: '语音输入' });
  await expect(microphone).toBeEnabled();
  await microphone.click();
  await expect(page.getByRole('button', { name: '停止录音' })).toBeVisible();
  await page.waitForTimeout(1_000);
  await page.getByRole('button', { name: '停止录音' }).click();
  await expect(input).toHaveValue('我在 Mirage 的 T 出生点，想封 VIP 烟', { timeout: 30_000 });
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('video[autoplay], audio')).toHaveCount(0);
});
