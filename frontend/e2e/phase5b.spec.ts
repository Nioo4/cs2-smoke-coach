import { expect, test, type Locator, type Page } from '@playwright/test';

test.describe('Phase 5B route_matches visible contract', () => {
  test.skip((process.env.TEST_AI_PROVIDER ?? 'fake') !== 'disabled', 'run after disabled backend recreation');
  test.setTimeout(120_000);

  const mediaRequests: string[] = [];

  async function clickNativePlay(page: Page, video: Locator) {
    await video.hover();
    const box = await video.boundingBox();
    expect(box).not.toBeNull();
    const playing = video.evaluate((element) => new Promise<boolean>((resolve, reject) => {
      if (!(element as HTMLVideoElement).paused) {
        resolve(true);
        return;
      }
      const timeout = window.setTimeout(() => reject(new Error('native play control did not start playback')), 5_000);
      element.addEventListener('playing', () => {
        window.clearTimeout(timeout);
        resolve(true);
      }, { once: true });
    }));
    await page.mouse.click(box!.x + 14, box!.y + box!.height - 30);
    await playing;
  }

  async function search(page: Page, text: string) {
    // The disabled-provider suite shares one Nginx client identity with the
    // preceding Phase 4/5A journeys. Respect the public 30 requests/minute
    // search contract instead of relying on whatever burst capacity remains.
    await page.waitForTimeout(2_200);
    mediaRequests.length = 0;
    await page.goto('/');
    await page.getByRole('textbox', { name: '搜索路线' }).fill(text);
    await page.getByRole('button', { name: '立即查找' }).click();
  }

  test('covers exact, target-only, origin-only, and catalog browse modes', async ({ page }) => {
    page.on('request', (request) => {
      try {
        const url = new URL(request.url());
        if (url.pathname.endsWith('.mp4')) mediaRequests.push(url.pathname);
      } catch {
        // Only same-origin media paths are evidence in this test.
      }
    });

    await search(page, '我在 T 出生点，想封警家烟');
    await expect(page.getByRole('heading', { name: 'T 出生点 → 警家' })).toBeVisible();

    await search(page, '警家烟怎么丢');
    await expect(page.getByRole('heading', { name: '警家 · 可用起点' })).toBeVisible();
    await assertRouteList(page, 1);

    await search(page, '从 T 出生点怎么丢烟');
    await expect(page.getByRole('heading', { name: /从T 出生点可到达的目标/ })).toBeVisible();
    await assertRouteList(page);

    await search(page, '烟怎么丢');
    await expect(page.getByRole('heading', { name: '全部已收录路线' })).toBeVisible();
    await assertRouteList(page);

    await search(page, '封不存在的点');
    await expect(page.getByText(/没有识别到已收录的目标位置/)).toBeVisible();
    await expect(page.getByRole('heading', { name: '全部已收录路线' })).not.toBeVisible();
    await expect(page.locator('video.route-choice-video')).toHaveCount(0);
  });

  async function assertRouteList(page: Page, minimumCount = 2) {
    const videos = page.locator('video.route-choice-video');
    const count = await videos.count();
    expect(count).toBeGreaterThanOrEqual(minimumCount);
    const beforePlayback = mediaRequests.length;
    expect(beforePlayback).toBe(0);
    for (let index = 0; index < count; index += 1) {
      const video = videos.nth(index);
      await expect(video).toHaveAttribute('preload', 'none');
      await expect(video).not.toHaveAttribute('autoplay');
      expect(await video.evaluate((element) => ({ paused: element.paused, currentTime: element.currentTime }))).toEqual({ paused: true, currentTime: 0 });
    }
    await expect.poll(() => mediaRequests.length).toBe(beforePlayback);

    const first = videos.nth(0);
    await clickNativePlay(page, first);
    await expect.poll(() => mediaRequests.length).toBeGreaterThan(beforePlayback);

    if (count > 1) {
      const second = videos.nth(1);
      await clickNativePlay(page, second);
      await expect.poll(() => first.evaluate((element) => element.paused)).toBe(true);
    }
    await page.getByRole('button', { name: /查看完整教学/ }).first().click();
    await expect(page.getByRole('button', { name: '← 返回起点列表' })).toBeVisible();
    await page.getByRole('button', { name: '← 返回起点列表' }).click();
    await expect(page.locator('video.route-choice-video').first()).toBeVisible();
  }
});
