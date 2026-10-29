import { expect, test } from '@playwright/test';

test('Phase 2 public shell exposes only the current local teaching controls', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('自动化测试环境 · 数据仅用于测试，非真实教学。')).toBeVisible();
  await expect(page.getByRole('heading', { name: '告诉我，你要封哪里。' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '搜索路线' })).toBeVisible();
  await expect(page.getByRole('button', { name: '立即查找' })).toBeVisible();
  await expect(page.getByRole('link', { name: '内容管理' })).toBeVisible();
  await expect(page.getByRole('button', { name: '语音输入' })).toHaveCount(1);
  await expect(page.getByText('地图定位')).toHaveCount(0);

  await page.getByRole('link', { name: '隐私' }).click();
  await expect(page.getByRole('heading', { name: '隐私说明' })).toBeVisible();
  await expect(page.getByText(/精确的本站目录查询不调用 AI/)).toBeVisible();
  await expect(page.getByText(/模糊中文表述.*阿里云百炼/)).toBeVisible();
  await expect(page.getByText(/首版不进行站外视频搜索/)).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '隐私说明' })).toBeVisible();
  await page.getByRole('link', { name: '非官方声明', exact: true }).click();
  await expect(page.getByRole('heading', { name: '非官方声明' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '非官方声明' })).toBeVisible();
  await page.getByRole('link', { name: '返回教学页' }).click();
  await expect(page.getByRole('heading', { name: '告诉我，你要封哪里。' })).toBeVisible();
});

for (const [width, height] of [[360, 800], [768, 1024], [1024, 768], [1440, 900]]) {
  test(`Phase 2 links and layout at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: '告诉我，你要封哪里。' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    expect(await page.locator('a').evaluateAll((links) => links.filter((link) => {
      const bounds = link.getBoundingClientRect();
      return bounds.width < 44 || bounds.height < 44;
    }).map((link) => link.textContent))).toEqual([]);
  });
}
