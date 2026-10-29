import { createHash } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { writePhase2Video } from './phase2-fixture';

test.describe.configure({ mode: 'serial' });

async function assertDrawerGeometry(page: import('@playwright/test').Page, width: number, height: number): Promise<void> {
  await page.setViewportSize({ width, height });
  await expect(page.locator(`.route-drawer.${width >= 1024 ? 'drawer-desktop' : 'drawer-mobile'}`)).toBeVisible();
  const geometry = await page.evaluate(() => {
    const drawer = document.querySelector<HTMLElement>('.route-drawer');
    const surface = document.querySelector<HTMLElement>('#public-surface');
    if (!drawer || !surface) return null;
    const drawerBox = drawer.getBoundingClientRect();
    const visibleControls = Array.from(surface.querySelectorAll<HTMLElement>('a,button,input,textarea,select')).filter((element) => {
      const box = element.getBoundingClientRect();
      return box.width > 0 && box.height > 0;
    });
    const overlaps = visibleControls
      .filter((element) => element.getBoundingClientRect().right > drawerBox.left + 0.5)
      .map((element) => element.getAttribute('aria-label') || element.textContent?.trim() || element.tagName);
    const touchSizes = Array.from(drawer.querySelectorAll<HTMLElement>('a,button,input,textarea,select,video')).map((element) => {
      const box = element.getBoundingClientRect();
      return { width: box.width, height: box.height };
    }).filter((box) => box.width > 0 && box.height > 0);
    return {
      drawerLeft: drawerBox.left,
      surfaceContentRight: document.querySelector<HTMLElement>('.home-page')?.getBoundingClientRect().right ?? 0,
      overlaps,
      maxDrawerHeight: drawerBox.height,
      viewportHeight: window.innerHeight,
      mobileInert: Boolean((surface as HTMLElement & { inert?: boolean }).inert),
      minTouchWidth: Math.min(...touchSizes.map((box) => box.width)),
      minTouchHeight: Math.min(...touchSizes.map((box) => box.height)),
    };
  });
  expect(geometry).not.toBeNull();
  if (!geometry) return;
  if (width >= 1024) {
    expect(geometry.overlaps, `visible controls overlap drawer at ${width}px: ${JSON.stringify(geometry)}`).toEqual([]);
    expect(geometry.surfaceContentRight).toBeLessThanOrEqual(geometry.drawerLeft);
  } else {
    expect(geometry.maxDrawerHeight).toBeLessThanOrEqual(height * 0.85 + 1);
    expect(geometry.mobileInert).toBe(true);
    expect(geometry.minTouchWidth).toBeGreaterThanOrEqual(44);
    expect(geometry.minTouchHeight).toBeGreaterThanOrEqual(44);
  }
}

async function paceAdminWrites(page: import('@playwright/test').Page): Promise<void> {
  // The real gateway allows one admin request per second with a finite burst.
  // Keep this workflow serial without changing the production limiter.
  await page.waitForTimeout(5_000);
}

async function setTargetPosition(
  page: import('@playwright/test').Page,
  targetSlug: string,
  mapX: number,
  mapY: number,
): Promise<{ map_x: number; map_y: number }> {
  await page.getByRole('tab', { name: '点位' }).click();
  const selector = page.locator('#target-position-select');
  await expect(selector).toBeVisible();
  const option = selector.locator('option').filter({ hasText: targetSlug }).first();
  const targetId = await option.getAttribute('value');
  expect(targetId).toBeTruthy();
  await selector.selectOption(targetId!);
  const svg = page.locator('.target-map-svg');
  await expect(svg).toBeVisible();
  await svg.scrollIntoViewIfNeeded();
  const screenPoint = await svg.evaluate((node, coordinates) => {
    const ctm = node.getScreenCTM();
    if (!ctm) throw new Error('Target SVG has no screen transform');
    // Keep a physical edge click inside the SVG hit area; the API still rounds the resulting viewBox point.
    const localX = Math.min(996, Math.max(4, coordinates.x * 10));
    const localY = Math.min(996, Math.max(4, coordinates.y * 10));
    const toScreen = () => new DOMPoint(localX, localY).matrixTransform(node.getScreenCTM()!);
    let point = toScreen();
    const viewportMargin = 8;
    if (point.y > window.innerHeight - viewportMargin) {
      window.scrollBy(0, point.y - (window.innerHeight - viewportMargin));
    } else if (point.y < viewportMargin) {
      window.scrollBy(0, point.y - viewportMargin);
    }
    point = toScreen();
    return { x: point.x, y: point.y };
  }, { x: mapX, y: mapY });
  await page.mouse.click(screenPoint.x, screenPoint.y);
  const previewLabel = page.getByText(/^预览坐标：\d+\.\d{2}, \d+\.\d{2}$/);
  await expect(previewLabel).toBeVisible();
  const previewMatch = (await previewLabel.innerText()).match(/^预览坐标：(\d+\.\d{2}), (\d+\.\d{2})$/);
  expect(previewMatch).not.toBeNull();
  const previewPosition = { map_x: Number(previewMatch![1]), map_y: Number(previewMatch![2]) };
  expect(previewPosition.map_x).toBeCloseTo(mapX, 0);
  expect(previewPosition.map_y).toBeCloseTo(mapY, 0);
  const saveButton = page.getByRole('button', { name: '保存目标位置' });
  await expect(saveButton).toBeEnabled();
  await saveButton.click();
  let savedPosition: { map_x: number; map_y: number } | null = null;
  await expect.poll(async () => {
    const response = await page.request.get('/api/v1/admin/targets', {
      headers: { Authorization: `Basic ${Buffer.from('admin:phase2-local-test-password', 'utf8').toString('base64')}`, Accept: 'application/json' },
    });
    const rows = await response.json() as Array<{ slug: string; map_x: number | null; map_y: number | null }>;
    const row = rows.find((item) => item.slug === targetSlug);
    savedPosition = row && row.map_x !== null && row.map_y !== null ? { map_x: row.map_x, map_y: row.map_y } : null;
    return savedPosition;
  }, { timeout: 30_000 }).toEqual(previewPosition);
  return savedPosition!;
}

let lastSearchDispatchAt = 0;

async function paceSearch(page: import('@playwright/test').Page): Promise<void> {
  // The real gateway allows 30 searches per minute with a finite burst.
  // Preserve that production boundary and space this long browser workflow accordingly.
  const remainingMs = 2_200 - (Date.now() - lastSearchDispatchAt);
  if (remainingMs > 0) {
    await page.waitForTimeout(remainingMs);
  }
  lastSearchDispatchAt = Date.now();
}

async function submitSearch(page: import('@playwright/test').Page): Promise<void> {
  await paceSearch(page);
  await page.getByRole('button', { name: '立即查找' }).click();
}

async function responseFingerprint(response: import('@playwright/test').APIResponse): Promise<string> {
  return createHash('sha256').update(await response.body()).digest('hex');
}

async function assertNoHorizontalOverflow(page: import('@playwright/test').Page): Promise<void> {
  const geometry = await page.evaluate(() => ({
    root: {
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    },
    drawers: Array.from(document.querySelectorAll<HTMLElement>('.drawer-body')).map((element) => ({
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
    })),
  }));
  expect(geometry.root.scrollWidth, `horizontal overflow: ${JSON.stringify(geometry)}`).toBeLessThanOrEqual(geometry.root.clientWidth);
  expect(geometry.drawers.filter((item) => item.scrollWidth > item.clientWidth), `drawer overflow: ${JSON.stringify(geometry)}`).toEqual([]);
}

test('管理员创建同位置双路线并由匿名玩家切换真实媒体', async ({ page }, testInfo) => {
  test.setTimeout(480_000);
  const expectedBaseUrl = 'http://nginx:8080';
  expect(process.env.BASE_URL).toBe(expectedBaseUrl);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.locator('.test-environment-banner')).toHaveText('自动化测试环境 · 数据仅用于测试，非真实教学。');
  const suffix = Date.now().toString(36);
  const targetSlug = 'vip';
  const originSlug = 't-spawn';
  const secondOriginSlug = 'a-ramp';
  const firstInputPath = testInfo.outputPath('phase2-input-a.mp4');
  const secondInputPath = testInfo.outputPath('phase2-input-b.mp4');
  await writePhase2Video(firstInputPath, 'primary');
  await writePhase2Video(secondInputPath, 'secondary');

  await page.goto('/admin');
  await page.getByLabel('用户名').fill('admin');
  await page.getByLabel('密码').fill('phase2-local-test-password');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page.getByRole('heading', { name: '把一条路线教清楚。' })).toBeVisible();
  await expect(page.locator('#lineup-method')).toHaveCount(0);
  await page.getByRole('tab', { name: '点位' }).click();
  await expect(page.getByRole('tabpanel', { name: '点位' })).toBeVisible();

  const targetForm = page.locator('form.compact-admin-form').filter({ has: page.locator('#target-slug') });
  await targetForm.locator('#target-slug').fill(targetSlug);
  await targetForm.locator('#target-name-zh').fill('VIP');
  await targetForm.locator('#target-name-en').fill('Window');
  await targetForm.locator('#target-description').fill('自动化测试夹具目标');
  await paceAdminWrites(page);
  await targetForm.getByRole('button', { name: '创建 Target' }).click();
  await expect(page.locator('.point-directory-item').filter({ hasText: targetSlug })).toHaveCount(1);

  const testAdminAuthorization = `Basic ${Buffer.from('admin:phase2-local-test-password', 'utf8').toString('base64')}`;
  const createdTargets = await page.request.get('/api/v1/admin/targets', {
    headers: { Authorization: testAdminAuthorization, Accept: 'application/json' },
  });
  expect(createdTargets.ok()).toBe(true);
  const targetRows = (await createdTargets.json()) as Array<{ slug: string; map_x: number | null; map_y: number | null }>;
  expect(targetRows.find((row) => row.slug === targetSlug)).toMatchObject({ map_x: null, map_y: null });
  const targetPosition = await setTargetPosition(page, targetSlug, 24, 36);

  const originForm = page.locator('form.compact-admin-form').filter({ has: page.locator('#origin-slug') });
  await originForm.locator('#origin-slug').fill(originSlug);
  await originForm.locator('#origin-name-zh').fill('T 出生点');
  await originForm.locator('#origin-name-en').fill('T Spawn');
  await originForm.locator('#origin-description').fill('自动化测试夹具起点');
  await paceAdminWrites(page);
  await originForm.getByRole('button', { name: '创建 Origin' }).click();
  await expect(page.locator('.point-directory-item').filter({ hasText: originSlug })).toHaveCount(1);

  const createdOrigins = await page.request.get('/api/v1/admin/origins', {
    headers: { Authorization: testAdminAuthorization, Accept: 'application/json' },
  });
  expect(createdOrigins.ok()).toBe(true);
  const originRows = (await createdOrigins.json()) as Array<{ slug: string; map_x: number | null; map_y: number | null }>;
  expect(originRows.find((row) => row.slug === originSlug)).toMatchObject({ map_x: null, map_y: null });

  const secondOriginForm = page.locator('form.compact-admin-form').filter({ has: page.locator('#origin-slug') });
  await secondOriginForm.locator('#origin-slug').fill(secondOriginSlug);
  await secondOriginForm.locator('#origin-name-zh').fill('A1');
  await secondOriginForm.locator('#origin-name-en').fill('A Ramp');
  await secondOriginForm.locator('#origin-description').fill('自动化测试夹具第二起点');
  await paceAdminWrites(page);
  await secondOriginForm.getByRole('button', { name: '创建 Origin' }).click();
  await expect(page.locator('.point-directory-item').filter({ hasText: secondOriginSlug })).toHaveCount(1);

  await page.getByRole('tab', { name: '路线' }).click();
  await expect(page.getByRole('tabpanel', { name: '路线' })).toBeVisible();
  await expect(page.locator('#lineup-target option').filter({ hasText: `VIP · ${targetSlug}` })).toHaveCount(1);
  await expect(page.locator('#lineup-origin option').filter({ hasText: `T 出生点 · ${originSlug}` })).toHaveCount(1);
  await expect(page.locator('#lineup-origin option').filter({ hasText: `A1 · ${secondOriginSlug}` })).toHaveCount(1);

  const targetId = await page.locator('#lineup-target option').filter({ hasText: `VIP · ${targetSlug}` }).getAttribute('value');
  const originId = await page.locator('#lineup-origin option').filter({ hasText: `T 出生点 · ${originSlug}` }).getAttribute('value');
  const secondOriginId = await page.locator('#lineup-origin option').filter({ hasText: `A1 · ${secondOriginSlug}` }).getAttribute('value');
  expect(targetId).toBeTruthy();
  expect(originId).toBeTruthy();
  expect(secondOriginId).toBeTruthy();

  type CreatedLineup = { id: string; setup: string; aim: string; throwText: string; result: string };

  async function createLineup(originValue: string, sortOrder: number, suffixText: string): Promise<CreatedLineup> {
    const text = {
      setup: `自动化测试站位${suffixText}${'X'.repeat(64)}`,
      aim: `自动化测试瞄点${suffixText}${'Y'.repeat(64)}`,
      throwText: `自动化测试普通投掷文字${suffixText}`,
      result: `自动化测试封烟${suffixText}`,
    };
    await page.locator('#lineup-target').selectOption(targetId!);
    await page.locator('#lineup-origin').selectOption(originValue);
    await page.locator('#new-lineup-setup').fill(text.setup);
    await page.locator('#new-lineup-aim').fill(text.aim);
    await page.locator('#new-lineup-throw').fill(text.throwText);
    await page.locator('#new-lineup-result').fill(text.result);
    await page.locator('#lineup-sort-order').fill(String(sortOrder));
    await paceAdminWrites(page);
    const cardCount = await page.locator('article.admin-lineup-card').count();
    await page.getByRole('button', { name: '保存草稿' }).click();
    await expect(page.locator('article.admin-lineup-card')).toHaveCount(cardCount + 1, { timeout: 30_000 });
    const card = page.locator('article.admin-lineup-card').filter({ hasText: text.setup });
    await expect(card).toHaveCount(1, { timeout: 30_000 });
    await expect(card).toContainText('编辑草稿');
    const heading = await card.locator('.lineup-card-heading .eyebrow').innerText();
    const id = heading.match(/ID ([0-9a-f-]{36})/)?.[1];
    expect(id).toMatch(/^[0-9a-f-]{36}$/);
    return { id: id!, ...text };
  }

  const first = await createLineup(originId!, 1, '一');
  const second = await createLineup(originId!, 99, '二');
  const third = await createLineup(secondOriginId!, 3, '三');
  const firstId = first.id;
  const secondId = second.id;
  expect(firstId).not.toBe(secondId);
  const lineupCard = (id: string) => page.locator('article.admin-lineup-card').filter({ hasText: `ID ${id}` });
  const firstCard = lineupCard(firstId);
  const secondCard = lineupCard(secondId);
  const thirdCard = lineupCard(third.id);

  await firstCard.locator('input[type="file"]').setInputFiles(firstInputPath);
  await firstCard.getByLabel('rights_note').fill('自动化测试夹具一，非真实教学');
  await paceAdminWrites(page);
  await firstCard.getByRole('button', { name: '上传主视频' }).click();
  await expect(firstCard).toContainText('视频状态：ready', { timeout: 150_000 });
  await expect(firstCard.getByText('主视频已就绪')).toBeVisible();

  await secondCard.locator('input[type="file"]').setInputFiles(secondInputPath);
  await secondCard.getByLabel('rights_note').fill('自动化测试夹具二，非真实教学');
  await paceAdminWrites(page);
  await secondCard.getByRole('button', { name: '上传主视频' }).click();
  await expect(secondCard).toContainText('视频状态：ready', { timeout: 150_000 });
  await expect(secondCard.getByText('主视频已就绪')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('admin-upload-ready.png'), fullPage: true });

  await secondCard.getByLabel('不设为默认（false）').check();
  await paceAdminWrites(page);
  await secondCard.getByRole('button', { name: '发布路线' }).click();
  await expect(secondCard).toContainText('同组第一条路线发布时必须设为默认。');
  await secondCard.getByLabel('设为默认（true）').check();
  await paceAdminWrites(page);
  await secondCard.getByRole('button', { name: '发布路线' }).click();
  await expect(secondCard).toContainText('已发布教学字段只读', { timeout: 30_000 });

  await thirdCard.locator('input[type="file"]').setInputFiles(firstInputPath);
  await thirdCard.getByLabel('rights_note').fill('自动化测试夹具三，非真实教学');
  await paceAdminWrites(page);
  await thirdCard.getByRole('button', { name: '上传主视频' }).click();
  await expect(thirdCard).toContainText('视频状态：ready', { timeout: 150_000 });
  await thirdCard.getByLabel('设为默认（true）').check();
  await paceAdminWrites(page);
  await thirdCard.getByRole('button', { name: '发布路线' }).click();
  await expect(thirdCard).toContainText('已发布教学字段只读', { timeout: 30_000 });

  await firstCard.getByLabel('不设为默认（false）').check();
  await paceAdminWrites(page);
  await firstCard.getByRole('button', { name: '发布路线' }).click();
  await expect(firstCard).toContainText('已发布教学字段只读', { timeout: 30_000 });

  let approvedMapForTest = false;
  await page.route('**/api/v1/maps/mirage', async (route) => {
    const response = await route.fetch();
    const body = await response.json() as { map: { asset_status: string }; [key: string]: unknown };
    body.map.asset_status = approvedMapForTest ? 'approved' : 'placeholder';
    await route.fulfill({ response, json: body });
  });
  const publicMap = await page.request.get('/api/v1/maps/mirage');
  expect(publicMap.ok()).toBe(true);
  const publicMapBody = await publicMap.json() as {
    published_lineup_count: number;
    targets: Array<{ slug: string; map_x: number | null; map_y: number | null; origin_count: number }>;
  };
  expect(publicMapBody.published_lineup_count).toBe(3);
  expect(publicMapBody.targets).toHaveLength(1);
  expect(publicMapBody.targets.find((row) => row.slug === targetSlug)).toMatchObject({
    map_x: targetPosition.map_x,
    map_y: targetPosition.map_y,
    origin_count: 2,
  });
  const publicTarget = await page.request.get(`/api/v1/targets/${targetSlug}/lineups`);
  expect(publicTarget.ok()).toBe(true);
  const publicTargetBody = await publicTarget.json() as {
    target: { map_x: number | null; map_y: number | null };
    origins: Array<{ origin: { map_x: number | null; map_y: number | null }; lineup: { id: string } }>;
  };
  expect(publicTargetBody.target).toMatchObject({ map_x: targetPosition.map_x, map_y: targetPosition.map_y });
  expect(publicTargetBody.origins).toHaveLength(2);
  expect(publicTargetBody.origins[0].origin).toMatchObject({ map_x: null, map_y: null });
  expect(publicTargetBody.origins[0].lineup.id).toBe(third.id);
  expect(publicTargetBody.origins[1].origin).toMatchObject({ map_x: null, map_y: null });
  expect(publicTargetBody.origins[1].lineup.id).toBe(secondId);
  const publicDetail = await page.request.get(`/api/v1/lineups/${secondId}`);
  expect(publicDetail.ok()).toBe(true);
  const publicDetailBody = await publicDetail.json() as {
    target: { map_x: number | null; map_y: number | null };
    origin: { map_x: number | null; map_y: number | null };
    other_lineups: Array<{ id: string }>;
    primary_video: { url: string };
  };
  expect(publicDetailBody.target).toMatchObject({ map_x: targetPosition.map_x, map_y: targetPosition.map_y });
  expect(publicDetailBody.origin).toMatchObject({ map_x: null, map_y: null });
  expect(publicDetailBody.other_lineups.map((item) => item.id)).toEqual([firstId]);
  await page.setViewportSize({ width: 360, height: 800 });
  await expect(firstCard).toContainText(`ID ${firstId}`);
  await expect(secondCard).toContainText(`ID ${secondId}`);
  await assertNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath('admin-upload-ready-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '退出' }).click();
  await expect(page.getByRole('heading', { name: '进入内容管理。' })).toBeVisible();

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.getByText('3 条可用路线', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '在地图上选择。' })).toHaveCount(0);
  approvedMapForTest = true;
  await page.reload();
  await expect(page.getByText('3 条可用路线', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '在地图上选择。' })).toBeVisible();
  const mapTarget = page.locator('.map-target-button').filter({ hasText: 'VIP' });
  await expect(mapTarget).toHaveCount(1);
  await mapTarget.click();
  await expect(page.locator('.route-choice-card')).toHaveCount(2);
  await expect(page.locator('.map-origin-button, .map-origin-card, .map-route-line')).toHaveCount(0);
  await page.getByRole('button', { name: 'T 出生点 → VIP：查看完整教学' }).click();
  await expect(page.locator('video')).toHaveCount(1);
  await expect(page.locator('video')).toHaveJSProperty('paused', true);
  await page.getByRole('button', { name: '关闭详情' }).click();

  const defaultMediaUrl = new URL(publicDetailBody.primary_video.url, page.url()).toString();
  const readAdminState = async (): Promise<Array<{ id: string; status: string; is_default: boolean; video_status: string | null }>> => {
    const response = await page.request.get('/api/v1/admin/lineups?status=all', {
      headers: { Authorization: testAdminAuthorization, Accept: 'application/json' },
    });
    expect(response.ok()).toBe(true);
    const rows = await response.json() as Array<{ id: string; status: string; is_default: boolean; primary_video: { status: string } | null }>;
    return rows.filter((row) => [firstId, secondId, third.id].includes(row.id)).map((row) => ({
      id: row.id,
      status: row.status,
      is_default: row.is_default,
      video_status: row.primary_video?.status ?? null,
    })).sort((a, b) => a.id.localeCompare(b.id));
  };

  const stateBeforeBrowserFailure = await readAdminState();
  await page.route(defaultMediaUrl, (route) => route.abort());
  await page.getByRole('textbox', { name: '搜索路线' }).fill('VIP');
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'VIP · 可用起点' })).toBeVisible();
  const routePreviewVideos = page.locator('video.route-choice-video');
  await expect(routePreviewVideos).toHaveCount(2);
  await expect(routePreviewVideos.nth(0)).toHaveJSProperty('paused', true);
  await expect(routePreviewVideos.nth(1)).toHaveJSProperty('paused', true);
  const originCards = page.locator('.origin-card');
  await expect(originCards).toHaveCount(2);
  const originCard = originCards.filter({ hasText: '自动化测试站位二' });
  await expect(originCard).toHaveCount(1);
  await originCard.getByRole('button', { name: /查看完整教学/ }).click();
  const defaultVideo = page.locator('video[aria-label="T 出生点 到 VIP 教学视频"]');
  await expect(page.getByText('教学视频暂不可用')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('button', { name: '一键换路线' })).toBeVisible();
  const failedDefaultHandle = await defaultVideo.elementHandle();
  expect(failedDefaultHandle).not.toBeNull();
  await page.getByRole('button', { name: '一键换路线' }).click();
  const switchedVideo = page.locator('video[aria-label="T 出生点 到 VIP 教学视频"]');
  const switchedHandle = await switchedVideo.elementHandle();
  expect(switchedHandle).not.toBeNull();
  await expect(switchedVideo).toHaveAttribute('src', new RegExp(firstId));
  await expect(switchedVideo).toHaveJSProperty('paused', true);
  await expect.poll(() => failedDefaultHandle!.evaluate((element) => ({ src: element.getAttribute('src'), paused: element.paused }))).toMatchObject({ src: null, paused: true });
  expect(await readAdminState()).toEqual(stateBeforeBrowserFailure);
  await page.getByRole('button', { name: '关闭详情' }).click();

  // A fresh search creates a fresh browser-failure set.  Keep the route
  // intercepted to prove retry remains local, then release it and retry the
  // same detail without changing the server-side video state.
  await page.getByRole('textbox', { name: '搜索路线' }).fill('VIP');
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'VIP · 可用起点' })).toBeVisible();
  await page.locator('.origin-card').filter({ hasText: '自动化测试站位二' })
    .getByRole('button', { name: /查看完整教学/ }).click();
  const retryVideo = page.locator('video[aria-label="T 出生点 到 VIP 教学视频"]');
  await expect(page.getByText('教学视频暂不可用')).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: '重试' }).click();
  await expect(page.getByText('教学视频暂不可用')).toBeVisible();
  await page.unroute(defaultMediaUrl);
  await page.getByRole('button', { name: '重试' }).click();
  await expect.poll(() => retryVideo.evaluate((element) => element.readyState), { timeout: 30_000 }).toBeGreaterThan(0);
  await expect(page.getByText('教学视频暂不可用')).toHaveCount(0);
  await expect(retryVideo).toHaveJSProperty('paused', true);
  expect(await readAdminState()).toEqual(stateBeforeBrowserFailure);
  const defaultVideoHandle = await retryVideo.elementHandle();
  expect(defaultVideoHandle).not.toBeNull();
  await expect(page.getByText(second.setup, { exact: true })).toBeVisible();
  await expect(page.getByText(second.aim, { exact: true })).toBeVisible();
  await expect(page.getByText(second.throwText, { exact: true })).toBeVisible();
  await expect(page.getByText(second.result, { exact: true })).toBeVisible();
  await assertDrawerGeometry(page, 1440, 900);
  await assertDrawerGeometry(page, 1024, 768);
  await assertDrawerGeometry(page, 360, 800);
  await page.screenshot({ path: testInfo.outputPath('mobile-route-detail.png'), fullPage: true });
  await assertDrawerGeometry(page, 768, 800);
  await assertDrawerGeometry(page, 1440, 900);
  await page.screenshot({ path: testInfo.outputPath('public-route-detail.png'), fullPage: true });

  await page.getByText('其他可用路线（1）').click();
  const otherRoute = page.locator('.other-route-button').filter({ hasText: '自动化测试站位一' });
  await expect(otherRoute).toHaveCount(1);
  await expect(otherRoute).toContainText('XXXXXXXXXXXXXXXXXXXX');
  await assertNoHorizontalOverflow(page);
  await otherRoute.click();
  const alternateVideo = page.locator('video[aria-label="T 出生点 到 VIP 教学视频"]');
  const alternateHandle = await alternateVideo.elementHandle();
  expect(alternateHandle).not.toBeNull();
  await expect(alternateVideo).toHaveAttribute('src', new RegExp(firstId));
  await expect(alternateVideo).toHaveJSProperty('paused', true);
  await expect(page.getByText(first.setup, { exact: true })).toBeVisible();
  await expect(page.getByText(first.aim, { exact: true })).toBeVisible();
  await expect(page.getByText(first.throwText, { exact: true })).toBeVisible();
  await expect(page.getByText(first.result, { exact: true })).toBeVisible();
  await expect.poll(() => defaultVideoHandle!.evaluate((element) => ({ src: element.getAttribute('src'), paused: element.paused }))).toMatchObject({ src: null, paused: true });
  await assertDrawerGeometry(page, 360, 800);
  await page.getByText('其他可用路线（1）').click();
  const expandedOtherRoute = page.locator('.other-route-button');
  await expect(expandedOtherRoute).toContainText('YYYYYYYYYYYYYYYYYYYY');
  await page.screenshot({ path: testInfo.outputPath('mobile-route-detail-long-hint.png'), fullPage: true });
  await assertNoHorizontalOverflow(page);
  await assertDrawerGeometry(page, 1440, 900);
  await expect(page.getByRole('button', { name: '← 返回起点列表' })).toBeVisible();

  const mediaUrl = new URL((await alternateVideo.getAttribute('src'))!, page.url()).toString();
  const defaultMedia = await page.request.get(defaultMediaUrl);
  expect(defaultMedia.status()).toBe(200);
  const defaultMediaFingerprint = await responseFingerprint(defaultMedia);
  const fullMedia = await page.request.get(mediaUrl);
  expect(fullMedia.status()).toBe(200);
  expect(fullMedia.headers()['cache-control']).toContain('immutable');
  const alternateMediaFingerprint = await responseFingerprint(fullMedia);
  expect(alternateMediaFingerprint).not.toBe(defaultMediaFingerprint);
  const rangedMedia = await page.request.get(mediaUrl, { headers: { Range: 'bytes=0-7' } });
  expect(rangedMedia.status()).toBe(206);
  expect(rangedMedia.headers()['content-range']).toMatch(/^bytes 0-7\//);
  expect(rangedMedia.headers()['cache-control']).toContain('immutable');
  const missingMedia = await page.request.get(new URL('/media/does-not-exist.mp4', page.url()).toString());
  expect(missingMedia.status()).toBe(404);
  expect(missingMedia.headers()['cache-control'] ?? '').not.toContain('immutable');
  const invalidRange = await page.request.get(mediaUrl, { headers: { Range: 'bytes=999999999-' } });
  expect(invalidRange.status()).toBe(416);
  expect(invalidRange.headers()['cache-control'] ?? '').not.toContain('immutable');
  const privateMedia = await page.request.get(new URL('/media/archive/private.mp4', page.url()).toString());
  expect(privateMedia.status()).toBe(404);
  await alternateVideo.click();
  await expect.poll(() => alternateVideo.evaluate((element) => element.ended), { timeout: 10_000 }).toBe(true);
  await expect.poll(() => alternateVideo.evaluate((element) => element.paused), { timeout: 5_000 }).toBe(true);
  await page.getByRole('button', { name: '← 返回起点列表' }).click();
  await expect(page.locator('.origin-card').filter({ hasText: '自动化测试站位二' })).toBeVisible();
  await expect(page.getByText('每组只展示公开默认路线。视频默认暂停，请主动播放；“查看完整教学”会打开文字步骤与其他路线。')).toBeVisible();
  await expect.poll(() => alternateHandle!.evaluate((element) => ({ src: element.getAttribute('src'), paused: element.paused }))).toMatchObject({ src: null, paused: true });

  await page.locator('.origin-card').filter({ hasText: '自动化测试站位二' })
    .getByRole('button', { name: /查看完整教学/ }).click();
  const reopenedVideo = page.locator('video[aria-label="T 出生点 到 VIP 教学视频"]');
  const reopenedHandle = await reopenedVideo.elementHandle();
  expect(reopenedHandle).not.toBeNull();
  await expect(reopenedVideo).toHaveAttribute('src', new RegExp(secondId));
  await expect(reopenedVideo).toHaveJSProperty('paused', true);
  await page.getByRole('button', { name: '关闭详情' }).click();
  await expect.poll(() => reopenedHandle!.evaluate((element) => ({ src: element.getAttribute('src'), paused: element.paused }))).toMatchObject({ src: null, paused: true });

  const noActionQuery = '我在 Mirage 的 T 出生点，想封 VIP';
  await page.getByRole('textbox', { name: '搜索路线' }).fill(noActionQuery);
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });
  const noActionVideo = page.locator('video[aria-label="T 出生点 到 VIP 教学视频"]');
  await expect(noActionVideo).toHaveAttribute('src', new RegExp(secondId));
  await expect(noActionVideo).toHaveJSProperty('paused', true);
  const noActionVideoSrc = await noActionVideo.getAttribute('src');
  const query = '我在 Mirage 的 T 出生点，想封 VIP 蹲着跳投';
  await page.getByRole('textbox', { name: '搜索路线' }).fill(query);
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });
  const explicitVideo = page.locator('video[aria-label="T 出生点 到 VIP 教学视频"]');
  await expect(explicitVideo).toHaveAttribute('src', new RegExp(secondId));
  expect(await explicitVideo.getAttribute('src')).toBe(noActionVideoSrc);
  await expect(explicitVideo).toHaveJSProperty('paused', true);
  await expect(explicitVideo).toHaveAttribute('preload', 'metadata');
  await expect(explicitVideo).not.toHaveAttribute('autoplay');

  await page.reload();
  await expect(page.getByRole('button', { name: 'VIP，2 个可用起点', exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole('textbox', { name: '搜索路线' }).fill(query);
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });

  // Phase 3 lifecycle acceptance stays in the browser: the admin page performs
  // every mutation, while a second anonymous page proves public visibility.
  await page.goto('/admin');
  await page.getByLabel('用户名').fill('admin');
  await page.getByLabel('密码').fill('phase2-local-test-password');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page.getByRole('heading', { name: '把一条路线教清楚。' })).toBeVisible();

  await paceAdminWrites(page);
  await secondCard.getByRole('button', { name: '下架' }).click();
  await expect(secondCard).toContainText('请先在另一条已发布路线设为默认。');

  await paceAdminWrites(page);
  await firstCard.getByRole('button', { name: '设为默认' }).click();
  await expect(firstCard.locator('.status-pill')).toHaveText('DEFAULT');
  await expect(secondCard.locator('.status-pill')).toHaveText('非默认');

  await paceAdminWrites(page);
  await secondCard.getByRole('button', { name: '下架' }).click();
  await expect(secondCard.getByRole('button', { name: '编辑草稿' })).toBeVisible();

  const lifecyclePage = await page.context().newPage();
  await lifecyclePage.setViewportSize({ width: 1440, height: 900 });
  await lifecyclePage.goto('/');
  await lifecyclePage.locator('.target-card').filter({ hasText: 'VIP' }).click();
  await expect(lifecyclePage.locator('.origin-card')).toHaveCount(2);
  const lifecycleTOriginCard = lifecyclePage.locator('.origin-card').filter({ hasText: 'T 出生点' });
  const lifecycleCtOriginCard = lifecyclePage.locator('.origin-card').filter({ hasText: 'A1' });
  await expect(lifecycleTOriginCard).toContainText('自动化测试站位一');

  await paceAdminWrites(page);
  await firstCard.getByRole('button', { name: '标记待复查' }).click();
  await expect(firstCard).toContainText('待复查');
  await lifecyclePage.reload();
  await lifecyclePage.locator('.target-card').filter({ hasText: 'VIP' }).click();
  await expect(lifecyclePage.locator('.origin-card')).toHaveCount(1);
  await expect(lifecycleTOriginCard).toHaveCount(0);
  await expect(lifecycleCtOriginCard).toHaveCount(1);

  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain(`归档路线 ${firstId}`);
    await dialog.accept();
  });
  await paceAdminWrites(page);
  await firstCard.getByRole('button', { name: '归档路线' }).click();
  await expect(firstCard.getByRole('button', { name: '恢复路线' })).toBeVisible();
  await expect(firstCard).toContainText('视频状态：archived');

  await paceAdminWrites(page);
  await firstCard.getByRole('button', { name: '恢复路线' }).click();
  await expect(firstCard.getByRole('button', { name: '恢复主视频' })).toBeVisible();
  await paceAdminWrites(page);
  await firstCard.getByRole('button', { name: '恢复主视频' }).click();
  await expect(firstCard).toContainText('视频状态：ready');
  await firstCard.getByLabel('设为默认（true）').check();
  await paceAdminWrites(page);
  await firstCard.getByRole('button', { name: '发布路线' }).click();
  await expect(firstCard.locator('.status-pill')).toHaveText('DEFAULT');

  await secondCard.getByLabel('不设为默认（false）').check();
  await paceAdminWrites(page);
  await secondCard.getByRole('button', { name: '发布路线' }).click();
  await expect(secondCard).toContainText('已发布教学字段只读');
  await expect(secondCard.locator('.status-pill')).toHaveText('非默认');

  await lifecyclePage.reload();
  await lifecyclePage.locator('.target-card').filter({ hasText: 'VIP' }).click();
  await expect(lifecyclePage.locator('.origin-card')).toHaveCount(2);
  await lifecycleTOriginCard.getByRole('button', { name: /查看完整教学/ }).click();
  await expect(lifecyclePage.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible();
  await expect(lifecyclePage.getByText('其他可用路线（1）')).toBeVisible();

  await page.getByRole('tab', { name: '点位' }).click();
  await expect(page.locator('.map-preview-section').getByText('APPROVED', { exact: true })).toBeVisible();
  await expect(page.getByRole('img', { name: 'Mirage 正式地图，点击设置 Target 预览位置' })).toBeVisible();

  const inUseTargetItem = page.locator('.point-directory-item').filter({ hasText: targetSlug });
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain(`Target“VIP”（${targetSlug}）`);
    await dialog.accept();
  });
  await paceAdminWrites(page);
  await inUseTargetItem.getByRole('button', { name: '归档' }).click();
  await expect(inUseTargetItem).toContainText('仍有路线引用该Target，无法归档。');

  const maintenanceSlug = `phase3-maintenance-${suffix}`;
  const maintenanceTargetForm = page.locator('form.compact-admin-form').filter({ has: page.locator('#target-slug') });
  await maintenanceTargetForm.locator('#target-slug').fill(maintenanceSlug);
  await maintenanceTargetForm.locator('#target-name-zh').fill('维护测试点');
  await maintenanceTargetForm.locator('#target-name-en').fill('Maintenance Point');
  await maintenanceTargetForm.locator('#target-description').fill('自动化测试点位生命周期');
  await paceAdminWrites(page);
  await maintenanceTargetForm.getByRole('button', { name: '创建 Target' }).click();
  const maintenanceItem = page.locator('.point-directory-item').filter({ hasText: maintenanceSlug });
  await expect(maintenanceItem).toHaveCount(1);
  const maintenanceFirstPosition = await setTargetPosition(page, maintenanceSlug, 50, 50);
  await expect(maintenanceItem).toContainText(`坐标 ${maintenanceFirstPosition.map_x}, ${maintenanceFirstPosition.map_y}`);
  const maintenanceFinalPosition = await setTargetPosition(page, maintenanceSlug, 0, 100);
  await maintenanceItem.getByRole('button', { name: '编辑' }).click();
  await maintenanceItem.getByLabel('别名').fill('维护点,maintenance-alias');
  await paceAdminWrites(page);
  await maintenanceItem.getByRole('button', { name: '保存点位' }).click();
  await expect(maintenanceItem).toContainText(`坐标 ${maintenanceFinalPosition.map_x}, ${maintenanceFinalPosition.map_y}`);
  await expect(maintenanceItem).toContainText('别名：maintenance-alias、维护点');

  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain(`Target“维护测试点”（${maintenanceSlug}）`);
    await dialog.accept();
  });
  await paceAdminWrites(page);
  await maintenanceItem.getByRole('button', { name: '归档' }).click();
  await expect(maintenanceItem).toHaveCount(0);
  await page.getByRole('checkbox', { name: '显示已归档' }).check();
  await expect(maintenanceItem).toContainText('已归档');
  await paceAdminWrites(page);
  await maintenanceItem.getByRole('button', { name: '恢复' }).click();
  await expect(maintenanceItem).toContainText('启用中');

  await page.getByRole('tab', { name: '指标' }).click();
  await expect(page.getByRole('heading', { name: '指标摘要。' })).toBeVisible();
  await expect(page.getByText('统计窗口')).toBeVisible();
  await expect(page.getByLabel('Provider 使用明细')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('phase3-lifecycle-metrics.png'), fullPage: true });

  // Phase 4 fixtures are created through the same visible admin workflow as
  // the Phase 1-3 fixtures. The fixed slugs are part of the isolated-stack
  // contract and make the fake provider's deterministic catalog safe to use.
  await page.getByRole('tab', { name: '点位' }).click();
  const phase4TargetForm = page.locator('form.compact-admin-form').filter({ has: page.locator('#target-slug') });
  await phase4TargetForm.locator('#target-slug').fill('ct-spawn');
  await phase4TargetForm.locator('#target-name-zh').fill('警家');
  await phase4TargetForm.locator('#target-name-en').fill('CT Spawn');
  await phase4TargetForm.locator('#target-description').fill('Phase 4 可见后台测试目标');
  await paceAdminWrites(page);
  await phase4TargetForm.getByRole('button', { name: '创建 Target' }).click();
  const ctSpawnItem = page.locator('.point-directory-item').filter({ hasText: 'ct-spawn' });
  await expect(ctSpawnItem).toHaveCount(1);
  await setTargetPosition(page, 'ct-spawn', 64, 24);

  await phase4TargetForm.locator('#target-slug').fill('bench');
  await phase4TargetForm.locator('#target-name-zh').fill('长椅');
  await phase4TargetForm.locator('#target-name-en').fill('Bench');
  await phase4TargetForm.locator('#target-description').fill('Phase 4 active no-local fixture');
  await paceAdminWrites(page);
  await phase4TargetForm.getByRole('button', { name: '创建 Target' }).click();
  const benchItem = page.locator('.point-directory-item').filter({ hasText: 'bench' });
  await expect(benchItem).toHaveCount(1);
  await setTargetPosition(page, 'bench', 40, 40);

  // E2E-12 fixture setup is visible before the no-local search checks.
  await page.getByRole('tab', { name: '点位' }).click();
  const vipItem = page.locator('.point-directory-item').filter({ hasText: 'vip' }).first();
  const warningItem = page.locator('.point-directory-item').filter({ hasText: 'ct-spawn' });
  await vipItem.getByRole('button', { name: '编辑' }).click();
  await vipItem.locator('input[id^="edit-point-aliases-"]').fill('窗口别名');
  await paceAdminWrites(page);
  await vipItem.getByRole('button', { name: '保存点位' }).click();
  await expect(vipItem).toContainText('别名：窗口别名');
  await warningItem.getByRole('button', { name: '编辑' }).click();
  await warningItem.locator('input[id^="edit-point-aliases-"]').fill('窗口别名');
  await paceAdminWrites(page);
  await warningItem.getByRole('button', { name: '保存点位' }).click();
  await expect(warningItem).toContainText('别名：窗口别名');

  await page.getByRole('tab', { name: '路线' }).click();
  const ctTargetId = await page.locator('#lineup-target option').filter({ hasText: '警家 · ct-spawn' }).getAttribute('value');
  const tSpawnId = await page.locator('#lineup-origin option').filter({ hasText: 'T 出生点 · t-spawn' }).getAttribute('value');
  expect(ctTargetId).toBeTruthy();
  expect(tSpawnId).toBeTruthy();
  await page.locator('#lineup-target').selectOption(ctTargetId!);
  await page.locator('#lineup-origin').selectOption(tSpawnId!);
  const phase4LineupText = {
    setup: `Phase 4 警家站位${'X'.repeat(64)}`,
    aim: `Phase 4 警家瞄点${'Y'.repeat(64)}`,
    throwText: 'Phase 4 警家普通投掷文字',
    result: 'Phase 4 警家封烟',
  };
  await page.locator('#new-lineup-setup').fill(phase4LineupText.setup);
  await page.locator('#new-lineup-aim').fill(phase4LineupText.aim);
  await page.locator('#new-lineup-throw').fill(phase4LineupText.throwText);
  await page.locator('#new-lineup-result').fill(phase4LineupText.result);
  await page.locator('#lineup-sort-order').fill('1');
  const phase4CardCount = await page.locator('article.admin-lineup-card').count();
  await paceAdminWrites(page);
  await page.getByRole('button', { name: '保存草稿' }).click();
  await expect(page.locator('article.admin-lineup-card')).toHaveCount(phase4CardCount + 1, { timeout: 30_000 });
  const phase4Card = page.locator('article.admin-lineup-card').filter({ hasText: phase4LineupText.setup });
  await expect(phase4Card).toHaveCount(1, { timeout: 30_000 });
  const phase4Heading = await phase4Card.locator('.lineup-card-heading .eyebrow').innerText();
  const phase4LineupId = phase4Heading.match(/ID ([0-9a-f-]{36})/)?.[1];
  expect(phase4LineupId).toMatch(/^[0-9a-f-]{36}$/);
  await phase4Card.locator('input[type="file"]').setInputFiles(firstInputPath);
  await phase4Card.getByLabel('rights_note').fill('Phase 4 自动化测试夹具，非真实教学');
  await paceAdminWrites(page);
  await phase4Card.getByRole('button', { name: '上传主视频' }).click();
  await expect(phase4Card).toContainText('视频状态：ready', { timeout: 150_000 });
  await phase4Card.getByLabel('设为默认（true）').check();
  await paceAdminWrites(page);
  await phase4Card.getByRole('button', { name: '发布路线' }).click();
  await expect(phase4Card).toContainText('已发布教学字段只读', { timeout: 30_000 });

  await page.getByRole('button', { name: '退出' }).click();
  await expect(page.getByRole('heading', { name: '进入内容管理。' })).toBeVisible();

  // E2E-04: deterministic follow-up replaces only Target and inherits Origin.
  await page.goto('/');
  let publicSearch = page.getByRole('textbox', { name: '搜索路线' });
  await publicSearch.fill('我在 Mirage 的 T 出生点，想封 VIP');
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });
  await publicSearch.fill('那改成警家烟');
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'T 出生点 → 警家' })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('#lineup-method')).toHaveCount(0);

  // E2E-05: Target-only and explicit Origin plus an action keep the curated
  // route surface and never expose a throw-method selector.
  await page.goto('/');
  publicSearch = page.getByRole('textbox', { name: '搜索路线' });
  await publicSearch.fill('VIP');
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'VIP · 可用起点' })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('.origin-card')).toHaveCount(2);
  await page.goto('/');
  publicSearch = page.getByRole('textbox', { name: '搜索路线' });
  await publicSearch.fill('我在 Mirage 的 T 出生点，想封 VIP 蹲着跳投');
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('#lineup-method')).toHaveCount(0);

  // E2E-06: no-local results remain explicit and do not create media embeds.
  async function assertLocalNoResult(): Promise<void> {
    await expect(page.locator('.notice-panel')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText('暂无符合该位置要求的站内路线，可换个起点或稍后再试。')).toBeVisible();
    await expect(page.locator('iframe, video, img')).toHaveCount(0);
    for (const [width, height] of [[360, 800], [768, 800], [1024, 768], [1440, 900]] as const) {
      await page.setViewportSize({ width, height });
      await assertNoHorizontalOverflow(page);
    }
  }

  await page.goto('/');
  publicSearch = page.getByRole('textbox', { name: '搜索路线' });
  await publicSearch.fill('T 出生点到长椅的跳投烟');
  await submitSearch(page);
  await assertLocalNoResult();

  await page.goto('/');
  publicSearch = page.getByRole('textbox', { name: '搜索路线' });
  await publicSearch.fill('长椅');
  await submitSearch(page);
  await assertLocalNoResult();

  // E2E-11: the fixed fake Intent phrase maps to T Spawn -> VIP, while fake
  // providers create no reservations.
  await page.goto('/');
  publicSearch = page.getByRole('textbox', { name: '搜索路线' });
  await publicSearch.fill('从匪家封中路看人的窗口');
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: 'T 出生点 → VIP' })).toBeVisible({ timeout: 30_000 });
  await page.goto('/admin');
  await page.getByLabel('用户名').fill('admin');
  await page.getByLabel('密码').fill('phase2-local-test-password');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page.getByRole('heading', { name: '把一条路线教清楚。' })).toBeVisible();
  await page.getByRole('tab', { name: '指标' }).click();
  const intentMetric = page.getByLabel('Intent 使用明细');
  await expect(intentMetric).toBeVisible();
  await expect(intentMetric.locator('dd').first()).toHaveText('0');

  // E2E-12: select the server-provided VIP value from the clarification.
  await page.getByRole('button', { name: '退出' }).click();
  await page.goto('/');
  publicSearch = page.getByRole('textbox', { name: '搜索路线' });
  await publicSearch.fill('窗口别名');
  await submitSearch(page);
  await expect(page.getByRole('heading', { name: '选择目标位置' })).toBeVisible({ timeout: 30_000 });
  const clarificationOptions = page.locator('.clarification-list .choice-button');
  await expect(clarificationOptions).toHaveCount(2);
  await expect(clarificationOptions).toHaveText(['VIP', '警家']);
  await paceSearch(page);
  await clarificationOptions.filter({ hasText: 'VIP' }).click();
  await expect(page.getByRole('heading', { name: 'VIP · 可用起点' })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('#lineup-method')).toHaveCount(0);
  await lifecyclePage.close();
});
