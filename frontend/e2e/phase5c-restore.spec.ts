import { expect, test } from '@playwright/test';

test.describe('Phase 5C isolated recovery Web smoke', () => {
  test.skip((process.env.TEST_AI_PROVIDER ?? 'disabled') !== 'disabled', 'recovery stack is deterministic only');
  test.setTimeout(60_000);

  test('serves the restored public route without provider calls', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('textbox', { name: '搜索路线' })).toBeVisible();
    await page.getByRole('textbox', { name: '搜索路线' }).fill('我在 T 出生点，想封警家烟');
    await page.getByRole('button', { name: '立即查找' }).click();
    await expect(page.getByRole('heading', { name: 'T 出生点 → 警家' })).toBeVisible();
    const video = page.locator('video.route-video');
    await expect(video).toBeVisible();
    await expect(video).not.toHaveAttribute('autoplay');
    await expect.poll(() => video.evaluate((element) => ({ paused: element.paused, currentTime: element.currentTime }))).toEqual({
      paused: true,
      currentTime: 0,
    });
    await video.hover();
    const box = await video.boundingBox();
    expect(box).not.toBeNull();
    const playing = video.evaluate((element) => new Promise<void>((resolve, reject) => {
      const media = element as HTMLVideoElement;
      if (!media.paused) {
        resolve();
        return;
      }
      const timeout = window.setTimeout(() => reject(new Error('restored video did not play')), 5_000);
      media.addEventListener('playing', () => {
        window.clearTimeout(timeout);
        resolve();
      }, { once: true });
    }));
    await page.mouse.click(box!.x + 14, box!.y + box!.height - 30);
    await playing;
    await expect.poll(() => video.evaluate((element) => element.currentTime)).toBeGreaterThan(0);
  });
});
