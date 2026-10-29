import { expect, test } from '@playwright/test';

test('5A disabled ASR error leaves the text route available', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('http://localhost:8080/');
  const input = page.getByRole('textbox', { name: '搜索路线' });
  const microphone = page.getByRole('button', { name: '语音输入' });
  await expect(microphone).toBeEnabled();
  await microphone.click();
  await expect(page.getByRole('button', { name: '停止录音' })).toBeVisible();
  await page.waitForTimeout(1_000);
  await page.getByRole('button', { name: '停止录音' }).click();
  await expect(page.getByText('语音识别服务暂不可用，请改用文字输入。')).toBeVisible({ timeout: 30_000 });

  await input.fill('我在 Mirage 的 T 出生点，想封 VIP');
  await page.getByRole('button', { name: '立即查找' }).click();
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });
  await expect(input).toHaveValue('我在 Mirage 的 T 出生点，想封 VIP');
});
