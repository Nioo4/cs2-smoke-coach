import { useCallback, useEffect, useRef, useState, type FormEvent, type MouseEvent as ReactMouseEvent } from 'react';
import { Link } from 'react-router-dom';

import { adminApi, ApiError } from '../api/client';
import { useAdminAuth } from '../app/adminAuth';
import type {
  AdminLineupOut,
  LineupCreatePayload,
  MapResponse,
  MetricsSummary,
  PointPatchPayload,
  PointCreatePayload,
  PointOut,
  TargetPositionPayload,
} from '../types/api';

interface PointFormState {
  slug: string;
  nameZh: string;
  nameEn: string;
  description: string;
  sortOrder: string;
  aliases: string;
}

interface LineupFormState {
  targetId: string;
  originId: string;
  setup: string;
  aim: string;
  throwText: string;
  result: string;
  sortOrder: string;
}

interface CheckInfoFormState {
  lastCheckedAt: string;
  gameVersionNote: string;
}

interface BatchLineupItem {
  origin_slug: string;
  target_slug: string;
  sort_order: number;
  source_file: string;
  steps: AdminLineupOut['steps'];
}

interface BatchImportSummary {
  items: BatchLineupItem[];
  sourceFiles: Set<string>;
}

const DEFAULT_RIGHTS_NOTE = '内容提供者本人录制的 Mirage 烟雾教学视频，提供用于 SmokeLine 教学展示；作者已确认自行验证。';

const emptyPoint: PointFormState = {
  slug: '', nameZh: '', nameEn: '', description: '', sortOrder: '0', aliases: '',
};
const emptyLineup: LineupFormState = {
  targetId: '', originId: '', setup: '', aim: '', throwText: '', result: '', sortOrder: '0',
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const fields = error.details && typeof error.details === 'object' && 'fields' in error.details
      ? (error.details as { fields?: Array<{ path?: string; reason?: string }> }).fields
      : undefined;
    const reasons: Record<string, string> = {
      missing: '缺少', too_many: '重复', too_many_parts: '部件过多', invalid_type: '类型不正确',
      invalid_value: '值不正确', invalid_text: '含不允许的控制字符', string_too_short: '太短', string_too_long: '太长',
    };
    const fieldText = fields?.filter((item) => item.path).map((item) => `${item.path}: ${reasons[item.reason ?? ''] ?? '不合法'}`).join('；');
    const missing = error.details && typeof error.details === 'object' && 'missing' in error.details
      ? (error.details as { missing?: string[] }).missing?.join('、') : undefined;
    const suffix = [fieldText, missing ? `缺少 ${missing}` : ''].filter(Boolean).join('；');
    return suffix ? `${error.message}（${suffix}）` : error.message;
  }
  return '网络暂时不可用，请保留表单后重试。';
}

function aliasesFromText(value: string): string[] {
  return value.split(/[\n,，]/).map((item) => item.trim()).filter(Boolean);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requiredText(value: unknown, path: string): string {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`清单字段 ${path} 必须是非空文字。`);
  return value.trim();
}

function parseBatchLineupManifest(value: unknown): BatchImportSummary {
  if (!isRecord(value) || !Array.isArray(value.lineups)) throw new Error('清单必须包含 lineups 数组。');
  const items: BatchLineupItem[] = [];
  const signatures = new Set<string>();
  const sourceFiles = new Set<string>();
  value.lineups.forEach((entry, index) => {
    if (!isRecord(entry)) throw new Error(`清单字段 lineups[${index}] 必须是对象。`);
    if (entry.action === 'replace_existing_video') return;
    if (entry.action !== 'create_upload_publish') throw new Error(`清单字段 lineups[${index}].action 不受支持。`);
    const path = `lineups[${index}]`;
    const originSlug = requiredText(entry.origin_slug, `${path}.origin_slug`);
    const targetSlug = requiredText(entry.target_slug, `${path}.target_slug`);
    const sourceFile = requiredText(entry.source_file, `${path}.source_file`);
    if (sourceFile.includes('/') || sourceFile.includes('\\')) throw new Error(`清单字段 ${path}.source_file 只能是文件名。`);
    if (!Number.isInteger(entry.sort_order) || Number(entry.sort_order) < 0 || Number(entry.sort_order) > 9999) {
      throw new Error(`清单字段 ${path}.sort_order 必须是 0–9999 的整数。`);
    }
    if (!isRecord(entry.steps)) throw new Error(`清单字段 ${path}.steps 必须是对象。`);
    const steps = {
      setup: requiredText(entry.steps.setup, `${path}.steps.setup`),
      aim: requiredText(entry.steps.aim, `${path}.steps.aim`),
      throw: requiredText(entry.steps.throw, `${path}.steps.throw`),
      result: requiredText(entry.steps.result, `${path}.steps.result`),
    };
    const signature = JSON.stringify([originSlug, targetSlug, entry.sort_order, steps]);
    if (signatures.has(signature)) throw new Error(`清单中存在完全重复的路线：${originSlug} → ${targetSlug}，优先级 ${entry.sort_order}。`);
    if (sourceFiles.has(sourceFile)) throw new Error(`清单中存在重复素材文件：${sourceFile}。`);
    signatures.add(signature);
    sourceFiles.add(sourceFile);
    items.push({ origin_slug: originSlug, target_slug: targetSlug, sort_order: Number(entry.sort_order), source_file: sourceFile, steps });
  });
  if (!items.length) throw new Error('清单中没有 action=create_upload_publish 的路线。');
  return { items, sourceFiles };
}

function sameSteps(left: AdminLineupOut['steps'], right: AdminLineupOut['steps']): boolean {
  return left.setup === right.setup && left.aim === right.aim && left.throw === right.throw && left.result === right.result;
}

function pointPayload(form: PointFormState): PointCreatePayload {
  return {
    slug: form.slug,
    name_zh: form.nameZh,
    name_en: form.nameEn || null,
    description: form.description,
    sort_order: Number(form.sortOrder),
    aliases: aliasesFromText(form.aliases),
  };
}

function statusLabel(status: AdminLineupOut['status']): string {
  return { draft: '草稿', published: '已发布', needs_revalidation: '待复查', archived: '已归档' }[status];
}

function coordinateLabel(point: PointOut): string {
  return point.map_x === null || point.map_y === null
    ? '待定位'
    : `坐标 ${point.map_x}, ${point.map_y}`;
}

type PointKind = 'target' | 'origin';
type AdminTab = 'lineups' | 'points' | 'metrics';

function pointPatchPayload(form: PointFormState): PointPatchPayload {
  return {
    name_zh: form.nameZh,
    name_en: form.nameEn || null,
    description: form.description,
    sort_order: Number(form.sortOrder),
    aliases: aliasesFromText(form.aliases),
  };
}

function checkInfoForm(checkInfo: AdminLineupOut['check_info']): CheckInfoFormState {
  const checkedAt = checkInfo.last_checked_at ? new Date(checkInfo.last_checked_at) : null;
  const localCheckedAt = checkedAt && !Number.isNaN(checkedAt.getTime())
    ? new Date(checkedAt.getTime() - checkedAt.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
    : '';
  return {
    lastCheckedAt: localCheckedAt,
    gameVersionNote: checkInfo.game_version_note ?? '',
  };
}

function checkInfoPayload(form: CheckInfoFormState): AdminLineupOut['check_info'] {
  return {
    last_checked_at: form.lastCheckedAt ? new Date(form.lastCheckedAt).toISOString() : null,
    game_version_note: form.gameVersionNote.trim() || null,
  };
}

function metricTime(value: string): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString('zh-CN', { hour12: false });
}

const emptyMetrics: MetricsSummary = {
  window_hours: 24,
  from: '',
  to: '',
  response_counts: { match: 0, route_matches: 0, clarification: 0, not_found: 0, out_of_scope: 0 },
  latency_ms: { p50: 0, p95: 0 },
  provider_usage: {
    intent: { attempts: 0, success: 0, failed: 0, unknown: 0, input_tokens: 0, output_tokens: 0 },
    asr: { attempts: 0, success: 0, failed: 0, unknown: 0, reserved_seconds: 0 },
  },
  not_found_combinations: [],
  errors: [],
};

export default function AdminPage() {
  const { authorization, authGeneration, login, clear } = useAdminAuth();
  if (!authorization) return <LoginPanel onLogin={login} />;
  return <AdminWorkspace authorization={authorization} authGeneration={authGeneration} onUnauthorized={clear} />;
}

function LoginPanel({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      await onLogin(username, password);
      setPassword('');
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="site-frame admin-frame">
      <header className="simple-header">
        <Link className="brand" to="/" aria-label="SmokeLine 首页">
          <span className="brand-mark" aria-hidden="true">SL</span><span>SmokeLine</span>
        </Link>
        <span className="scope-pill">内容管理</span>
      </header>
      <main className="page-shell admin-login" aria-labelledby="admin-login-title">
        <p className="eyebrow">ADMIN / SIGN IN</p>
        <h1 id="admin-login-title">进入内容管理。</h1>
        <p className="admin-lead">管理员凭据只在当前页面内存中使用；刷新或退出后需要重新登录。</p>
        <form className="admin-form login-form" onSubmit={submit}>
          <label htmlFor="admin-username">用户名</label>
          <input id="admin-username" name="username" type="text" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} />
          <label htmlFor="admin-password">密码</label>
          <input id="admin-password" name="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
          {error && <p className="inline-error" role="alert">{error}</p>}
          <button className="button button-primary" type="submit" disabled={busy}>{busy ? '验证中…' : '登录'}</button>
        </form>
        <Link className="back-link" to="/">← 返回教学页</Link>
      </main>
    </div>
  );
}

function AdminWorkspace({ authorization, authGeneration, onUnauthorized }: { authorization: string; authGeneration: number; onUnauthorized: (expectedAuthorization?: string, expectedGeneration?: number) => void }) {
  const [activeTab, setActiveTab] = useState<AdminTab>('lineups');
  const [includeArchived, setIncludeArchived] = useState(false);
  const [targets, setTargets] = useState<PointOut[]>([]);
  const [origins, setOrigins] = useState<PointOut[]>([]);
  const [lineups, setLineups] = useState<AdminLineupOut[]>([]);
  const [metrics, setMetrics] = useState<MetricsSummary | null>(null);
  const [mapCatalog, setMapCatalog] = useState<MapResponse['map'] | null>(null);
  const [mapCatalogError, setMapCatalogError] = useState('');
  const [metricsLoading, setMetricsLoading] = useState(false);
  const [metricsError, setMetricsError] = useState('');
  const [targetForm, setTargetForm] = useState(emptyPoint);
  const [originForm, setOriginForm] = useState(emptyPoint);
  const [lineupForm, setLineupForm] = useState(emptyLineup);
  const [pointError, setPointError] = useState('');
  const [lineupError, setLineupError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [loadingData, setLoadingData] = useState(true);
  const [lineupsLoaded, setLineupsLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [busyLineupId, setBusyLineupId] = useState<string | null>(null);
  const [lineupErrors, setLineupErrors] = useState<Record<string, string>>({});
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingSteps, setEditingSteps] = useState<AdminLineupOut['steps'] | null>(null);
  const [editingSortOrder, setEditingSortOrder] = useState<string | null>(null);
  const [editingCheckInfo, setEditingCheckInfo] = useState<CheckInfoFormState | null>(null);
  const [priorityDrafts, setPriorityDrafts] = useState<Record<string, string>>({});
  const [publishChoice, setPublishChoice] = useState<Record<string, boolean | undefined>>({});
  const [publishErrors, setPublishErrors] = useState<Record<string, string>>({});
  const [uploadFiles, setUploadFiles] = useState<Record<string, File | undefined>>({});
  const [uploadRights, setUploadRights] = useState<Record<string, string>>({});
  const [uploadErrors, setUploadErrors] = useState<Record<string, string>>({});
  const [uploadInputVersion, setUploadInputVersion] = useState<Record<string, number>>({});
  const [batchManifest, setBatchManifest] = useState<File | undefined>();
  const [batchVideos, setBatchVideos] = useState<File[]>([]);
  const [batchRights, setBatchRights] = useState(DEFAULT_RIGHTS_NOTE);
  const [batchInputVersion, setBatchInputVersion] = useState(0);
  const [batchStatus, setBatchStatus] = useState('');
  const [batchError, setBatchError] = useState('');
  const [editingPoint, setEditingPoint] = useState<{ kind: PointKind; id: string } | null>(null);
  const [pointEditErrors, setPointEditErrors] = useState<Record<string, string>>({});
  const loadGeneration = useRef(0);
  const loadController = useRef<AbortController | null>(null);
  const mapCatalogController = useRef<AbortController | null>(null);
  const adminRequestTail = useRef<Promise<void>>(Promise.resolve());

  const call = useCallback(async <T,>(operation: () => Promise<T>): Promise<T | null> => {
    const previous = adminRequestTail.current;
    let release = (): void => undefined;
    adminRequestTail.current = new Promise<void>((resolve) => { release = resolve; });
    await previous;
    try {
      return await operation();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized(authorization, authGeneration);
      throw error;
    } finally {
      release();
    }
  }, [authGeneration, authorization, onUnauthorized]);

  const loadAll = useCallback(async (): Promise<void> => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    const generation = ++loadGeneration.current;
    setLoadError('');
    setLoadingData(true);
    setLineupsLoaded(false);
    const result = await Promise.allSettled([
      call(() => adminApi.targets(authorization, controller.signal, includeArchived)),
      call(() => adminApi.origins(authorization, controller.signal, includeArchived)),
      call(() => adminApi.lineups(authorization, controller.signal)),
    ]);
    if (generation !== loadGeneration.current || controller.signal.aborted) return;
    const failures: unknown[] = [];
    const apply = <T,>(item: PromiseSettledResult<T | null>, setter: (value: T) => void): void => {
      if (item.status === 'fulfilled' && item.value !== null) setter(item.value);
      else if (item.status === 'rejected') failures.push(item.reason);
    };
    apply(result[0], setTargets);
    apply(result[1], setOrigins);
    apply(result[2], setLineups);
    setLineupsLoaded(result[2].status === 'fulfilled' && result[2].value !== null);
    setLoadError(failures.length ? errorText(failures[0]) : '');
    setLoadingData(false);
  }, [authorization, call, includeArchived]);

  const loadLineups = useCallback(async (): Promise<void> => {
    setLineupsLoaded(false);
    const value = await call(() => adminApi.lineups(authorization));
    if (value) {
      setLineups(value);
      setLineupsLoaded(true);
    }
  }, [authorization, call]);

  const loadPointDirectory = useCallback(async (kind: PointKind): Promise<void> => {
    const value = kind === 'target'
      ? await call(() => adminApi.targets(authorization, undefined, includeArchived))
      : await call(() => adminApi.origins(authorization, undefined, includeArchived));
    if (value) {
      if (kind === 'target') setTargets(value);
      else setOrigins(value);
    }
  }, [authorization, call, includeArchived]);

  useEffect(() => {
    void loadAll();
    return () => {
      loadGeneration.current += 1;
      loadController.current?.abort();
      loadController.current = null;
    }
  }, [loadAll]);

  const loadMapCatalog = useCallback(() => {
    mapCatalogController.current?.abort();
    const controller = new AbortController();
    mapCatalogController.current = controller;
    setMapCatalog(null);
    setMapCatalogError('');
    void call(() => adminApi.map(authorization, controller.signal)).then((value) => {
      if (!controller.signal.aborted && value) setMapCatalog(value);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setMapCatalogError(errorText(error));
    });
    return () => {
      controller.abort();
      if (mapCatalogController.current === controller) mapCatalogController.current = null;
    };
  }, [authorization, call]);

  useEffect(() => loadMapCatalog(), [loadMapCatalog]);

  const loadMetrics = useCallback(() => {
    const controller = new AbortController();
    setMetricsLoading(true);
    setMetricsError('');
    void call(() => adminApi.metrics(authorization, controller.signal)).then((value) => {
      if (!controller.signal.aborted && value) setMetrics(value);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setMetricsError(errorText(error));
    }).finally(() => {
      if (!controller.signal.aborted) setMetricsLoading(false);
    });
    return () => controller.abort();
  }, [authorization, call]);

  useEffect(() => {
    if (activeTab !== 'metrics') return undefined;
    return loadMetrics();
  }, [activeTab, loadMetrics]);

  async function createPoint(kind: 'target' | 'origin', event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setPointError('');
    setBusy(true);
    const form = kind === 'target' ? targetForm : originForm;
    const label = kind === 'target' ? 'Target' : 'Origin';
    if (!form.slug.trim() || !form.nameZh.trim()) {
      setPointError(`${label} 的 slug 和中文名称为必填项。`);
      setBusy(false);
      return;
    }
    try {
      const created = kind === 'target'
        ? await call(() => adminApi.createTarget(authorization, pointPayload(form)))
        : await call(() => adminApi.createOrigin(authorization, pointPayload(form)));
      if (created) {
        if (kind === 'target') setTargetForm(emptyPoint);
        else setOriginForm(emptyPoint);
        await loadPointDirectory(kind);
      }
    } catch (error) {
      setPointError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function createLineup(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setLineupError('');
    setBusy(true);
    const payload: LineupCreatePayload = {
      target_id: lineupForm.targetId,
      origin_id: lineupForm.originId,
      steps: {
        setup: lineupForm.setup,
        aim: lineupForm.aim,
        throw: lineupForm.throwText,
        result: lineupForm.result,
      },
      sort_order: Number(lineupForm.sortOrder),
      check_info: { last_checked_at: null, game_version_note: null },
    };
    try {
      const created = await call(() => adminApi.createLineup(authorization, payload));
      if (created) {
        setLineupForm(emptyLineup);
        await loadLineups();
      }
    } catch (error) {
      setLineupError(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function saveTeaching(lineupId: string): Promise<void> {
    if (!editingSteps || editingSortOrder === null || busyLineupId) return;
    setLineupErrors((old) => ({ ...old, [lineupId]: '' }));
    setBusyLineupId(lineupId);
    try {
      const sortOrder = Number(editingSortOrder);
      if (!Number.isInteger(sortOrder) || sortOrder < 0 || sortOrder > 9999) {
        throw new ApiError('请求参数不合法。', 422, 'VALIDATION_ERROR', { fields: [{ path: 'sort_order', reason: 'invalid_value' }] }, null);
      }
      await call(() => adminApi.patchLineup(authorization, lineupId, {
        steps: editingSteps,
        sort_order: sortOrder,
      }));
      await loadLineups();
    } catch (error) {
      setLineupErrors((old) => ({ ...old, [lineupId]: errorText(error) }));
    } finally {
      setBusyLineupId(null);
    }
  }

  async function saveCheckInfo(lineupId: string): Promise<void> {
    if (!editingCheckInfo || busyLineupId) return;
    setLineupErrors((old) => ({ ...old, [lineupId]: '' }));
    setBusyLineupId(lineupId);
    try {
      if (editingCheckInfo.lastCheckedAt && Number.isNaN(new Date(editingCheckInfo.lastCheckedAt).getTime())) {
        throw new ApiError('请求参数不合法。', 422, 'VALIDATION_ERROR', { fields: [{ path: 'check_info.last_checked_at', reason: 'invalid_value' }] }, null);
      }
      const checkInfo = checkInfoPayload(editingCheckInfo);
      await call(() => adminApi.patchLineup(authorization, lineupId, {
        check_info: checkInfo,
      }));
      await loadLineups();
    } catch (error) {
      setLineupErrors((old) => ({ ...old, [lineupId]: errorText(error) }));
    } finally {
      setBusyLineupId(null);
    }
  }

  async function savePriority(lineup: AdminLineupOut): Promise<void> {
    if (busyLineupId) return;
    const value = priorityDrafts[lineup.id] ?? String(lineup.sort_order);
    const sortOrder = Number(value);
    if (!Number.isInteger(sortOrder) || sortOrder < 0 || sortOrder > 9999) {
      setLineupErrors((old) => ({ ...old, [lineup.id]: '优先级必须是 0–9999 的整数。' }));
      return;
    }
    setLineupErrors((old) => ({ ...old, [lineup.id]: '' }));
    setBusyLineupId(lineup.id);
    try {
      await call(() => adminApi.patchLineup(authorization, lineup.id, { sort_order: sortOrder }));
      await loadLineups();
    } catch (error) {
      setLineupErrors((old) => ({ ...old, [lineup.id]: errorText(error) }));
    } finally {
      setBusyLineupId(null);
    }
  }

  async function upload(lineup: AdminLineupOut): Promise<void> {
    if (busyLineupId) return;
    const file = uploadFiles[lineup.id];
    const rights = uploadRights[lineup.id] ?? '';
    if (!file) {
      setUploadErrors((old) => ({ ...old, [lineup.id]: '请选择一个 MP4、WebM 或 MOV 视频文件。' }));
      return;
    }
    if (!rights.trim()) {
      setUploadErrors((old) => ({ ...old, [lineup.id]: '请填写 rights_note。' }));
      return;
    }
    setUploadErrors((old) => ({ ...old, [lineup.id]: '' }));
    setBusyLineupId(lineup.id);
    try {
      await call(() => adminApi.uploadVideo(authorization, lineup.id, file, rights));
      setUploadFiles((old) => ({ ...old, [lineup.id]: undefined }));
      setUploadRights((old) => ({ ...old, [lineup.id]: '' }));
      setUploadInputVersion((old) => ({ ...old, [lineup.id]: (old[lineup.id] ?? 0) + 1 }));
      await loadLineups();
    } catch (error) {
      setUploadErrors((old) => ({ ...old, [lineup.id]: errorText(error) }));
    } finally {
      setBusyLineupId(null);
    }
  }

  async function importBatch(): Promise<void> {
    if (busy || busyLineupId) return;
    setBatchError('');
    setBatchStatus('');
    if (!batchManifest) {
      setBatchError('请选择一份 JSON 内容清单。');
      return;
    }
    if (!batchRights.trim()) {
      setBatchError('请填写整批素材的 rights_note。');
      return;
    }
    setBusy(true);
    setBusyLineupId('__batch_import__');
    let completed = 0;
    let createdCount = 0;
    let uploadedCount = 0;
    let skippedCount = 0;
    try {
      let raw: unknown;
      try {
        raw = JSON.parse(await batchManifest.text());
      } catch {
        throw new Error('内容清单不是有效的 JSON 文件。');
      }
      const manifest = parseBatchLineupManifest(raw);
      const filesByName = new Map<string, File>();
      for (const file of batchVideos) {
        if (filesByName.has(file.name)) throw new Error(`重复选择了素材文件：${file.name}。`);
        filesByName.set(file.name, file);
      }
      const missingFiles = [...manifest.sourceFiles].filter((name) => !filesByName.has(name));
      const extraFiles = [...filesByName.keys()].filter((name) => !manifest.sourceFiles.has(name));
      if (missingFiles.length || extraFiles.length) {
        const details = [
          missingFiles.length ? `缺少：${missingFiles.join('、')}` : '',
          extraFiles.length ? `多选：${extraFiles.join('、')}` : '',
        ].filter(Boolean).join('；');
        throw new Error(`所选视频与清单不完全一致（${details}）。`);
      }

      const targetBySlug = new Map(targets.filter((point) => point.is_active).map((point) => [point.slug, point]));
      const originBySlug = new Map(origins.filter((point) => point.is_active).map((point) => [point.slug, point]));
      const plans = manifest.items.map((item) => {
        const target = targetBySlug.get(item.target_slug);
        const origin = originBySlug.get(item.origin_slug);
        if (!target) throw new Error(`找不到启用中的 Target：${item.target_slug}。`);
        if (!origin) throw new Error(`找不到启用中的 Origin：${item.origin_slug}。`);
        const matches = lineups.filter((lineup) => lineup.target.id === target.id && lineup.origin.id === origin.id && lineup.sort_order === item.sort_order && sameSteps(lineup.steps, item.steps));
        if (matches.length > 1) throw new Error(`后台存在多条与清单完全一致的路线：${item.origin_slug} → ${item.target_slug}，优先级 ${item.sort_order}。`);
        const lineup = matches[0];
        if (lineup?.status !== undefined && lineup.status !== 'draft') throw new Error(`已有路线 ${lineup.id} 不是草稿，批量导入不会修改它。`);
        if (lineup?.primary_video?.status !== undefined && lineup.primary_video.status !== 'ready') throw new Error(`路线 ${lineup.id} 已有非 ready 主视频，批量导入不会覆盖。`);
        if (lineup?.primary_video?.rights_note !== undefined && lineup.primary_video.rights_note !== batchRights.trim()) throw new Error(`路线 ${lineup.id} 的既有 rights_note 与本批不一致。`);
        return { item, target, origin, lineup };
      });

      for (const plan of plans) {
        const { item, target, origin } = plan;
        setBatchStatus(`正在处理 ${completed + 1}/${manifest.items.length}：${item.origin_slug} → ${item.target_slug} · ${item.source_file}`);
        let lineup = plan.lineup;
        if (!lineup) {
          const created = await call(() => adminApi.createLineup(authorization, {
            target_id: target.id,
            origin_id: origin.id,
            steps: item.steps,
            sort_order: item.sort_order,
            check_info: { last_checked_at: null, game_version_note: null },
          }));
          if (!created) throw new Error('管理员会话已失效，导入已停止。');
          lineup = created;
          createdCount += 1;
        }
        if (lineup.primary_video) {
          skippedCount += 1;
        } else {
          const source = filesByName.get(item.source_file);
          if (!source) throw new Error(`找不到素材文件：${item.source_file}。`);
          const uploaded = await call(() => adminApi.uploadVideo(authorization, lineup.id, source, batchRights.trim()));
          if (!uploaded || uploaded.primary_video?.status !== 'ready') throw new Error(`路线 ${lineup.id} 的主视频未进入 ready 状态。`);
          uploadedCount += 1;
        }
        completed += 1;
      }
      await loadLineups();
      setBatchManifest(undefined);
      setBatchVideos([]);
      setBatchInputVersion((value) => value + 1);
      setBatchStatus(`导入完成：${completed}/${manifest.items.length}；新建 ${createdCount} 条，上传 ${uploadedCount} 个视频，复用 ${skippedCount} 条已就绪草稿。全部保持草稿，尚未发布。`);
    } catch (error) {
      setBatchError(`${error instanceof Error ? error.message : errorText(error)} 已完成 ${completed} 条；修正后可用同一清单安全重试。`);
      await loadLineups().catch(() => undefined);
    } finally {
      setBusyLineupId(null);
      setBusy(false);
    }
  }

  async function publish(lineup: AdminLineupOut): Promise<void> {
    if (busyLineupId) return;
    const choice = publishChoice[lineup.id];
    if (choice === undefined) {
      setPublishErrors((old) => ({ ...old, [lineup.id]: '请明确选择是否设为默认，再发布。' }));
      return;
    }
    setPublishErrors((old) => ({ ...old, [lineup.id]: '' }));
    setBusyLineupId(lineup.id);
    try {
      await call(() => adminApi.publish(authorization, lineup.id, choice));
      await loadLineups();
    } catch (error) {
      setPublishErrors((old) => ({ ...old, [lineup.id]: errorText(error) }));
    } finally {
      setBusyLineupId(null);
    }
  }

  async function lineupAction(
    lineup: AdminLineupOut,
    action: 'setDefault' | 'unpublish' | 'markNeeds' | 'archive' | 'restore' | 'archiveVideo' | 'restoreVideo',
  ): Promise<void> {
    if (busyLineupId) return;
    const actionLabel = action === 'archive' ? `归档路线 ${lineup.id}（${lineup.origin.name_zh} → ${lineup.target.name_zh}）？` : action === 'archiveVideo' ? `归档路线 ${lineup.id} 的主视频？` : '';
    if (actionLabel && !window.confirm(actionLabel)) return;
    setLineupErrors((old) => ({ ...old, [lineup.id]: '' }));
    setBusyLineupId(lineup.id);
    try {
      const operation = {
        setDefault: adminApi.setDefault,
        unpublish: adminApi.unpublish,
        markNeeds: adminApi.markNeeds,
        archive: adminApi.archiveLineup,
        restore: adminApi.restoreLineup,
        archiveVideo: adminApi.archiveVideo,
        restoreVideo: adminApi.restoreVideo,
      }[action];
      await call(() => operation(authorization, lineup.id));
      await loadLineups();
    } catch (error) {
      setLineupErrors((old) => ({ ...old, [lineup.id]: errorText(error) }));
    } finally {
      setBusyLineupId(null);
    }
  }

  async function editPoint(kind: PointKind, point: PointOut, form: PointFormState): Promise<void> {
    setPointEditErrors((old) => ({ ...old, [point.id]: '' }));
    setBusy(true);
    try {
      const operation = kind === 'target' ? adminApi.patchTarget : adminApi.patchOrigin;
      await call(() => operation(authorization, point.id, pointPatchPayload(form)));
      setEditingPoint(null);
      await loadPointDirectory(kind);
    } catch (error) {
      setPointEditErrors((old) => ({ ...old, [point.id]: errorText(error) }));
    } finally {
      setBusy(false);
    }
  }

  async function pointAction(kind: PointKind, point: PointOut, archive: boolean): Promise<void> {
    if (busy) return;
    if (archive && !window.confirm(`确定归档${kind === 'target' ? 'Target' : 'Origin'}“${point.name_zh}”（${point.slug}）吗？`)) return;
    setBusy(true);
    setPointEditErrors((old) => ({ ...old, [point.id]: '' }));
    try {
      const operation = kind === 'target'
        ? (archive ? adminApi.archiveTarget : adminApi.restoreTarget)
        : (archive ? adminApi.archiveOrigin : adminApi.restoreOrigin);
      await call(() => operation(authorization, point.id));
      await loadPointDirectory(kind);
    } catch (error) {
      setPointEditErrors((old) => ({ ...old, [point.id]: errorText(error) }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="site-frame admin-frame">
      <header className="topbar admin-topbar">
        <Link className="brand" to="/" aria-label="SmokeLine 首页"><span className="brand-mark" aria-hidden="true">SL</span><span>SmokeLine</span></Link>
        <span className="scope-pill">内容管理</span>
        {import.meta.env.MODE === 'test' && <span className="test-badge">自动化测试环境</span>}
        <button className="text-button logout-button" type="button" onClick={() => onUnauthorized()}>退出</button>
      </header>
      <main className="page-shell admin-page" aria-labelledby="admin-title">
        <section className="admin-heading">
          <p className="eyebrow">ADMIN / CURATED CONTENT</p>
          <h1 id="admin-title">把一条路线教清楚。</h1>
          <p className="admin-lead">维护目录、路线、一个主视频与显式默认；已发布教学字段须先下架后修改。</p>
        </section>
        <nav className="admin-tabs" role="tablist" aria-label="管理标签">
          <button id="admin-tab-lineups" className={activeTab === 'lineups' ? 'is-active' : ''} role="tab" type="button" aria-controls="admin-panel-lineups" aria-selected={activeTab === 'lineups'} onClick={() => setActiveTab('lineups')}>路线</button>
          <button id="admin-tab-points" className={activeTab === 'points' ? 'is-active' : ''} role="tab" type="button" aria-controls="admin-panel-points" aria-selected={activeTab === 'points'} onClick={() => setActiveTab('points')}>点位</button>
          <button id="admin-tab-metrics" className={activeTab === 'metrics' ? 'is-active' : ''} role="tab" type="button" aria-controls="admin-panel-metrics" aria-selected={activeTab === 'metrics'} onClick={() => setActiveTab('metrics')}>指标</button>
        </nav>
        {loadError && <p className="inline-error" role="alert">{loadError}</p>}
        {pointError && <p className="inline-error" role="alert">{pointError}</p>}

        {activeTab === 'points' && <section id="admin-panel-points" role="tabpanel" aria-labelledby="admin-tab-points">
          <section className="admin-grid" aria-label="目录管理">
            <PointForm title="创建 Target" prefix="target" form={targetForm} setForm={setTargetForm} onSubmit={(event) => void createPoint('target', event)} busy={busy} />
            <PointForm title="创建 Origin" prefix="origin" form={originForm} setForm={setOriginForm} onSubmit={(event) => void createPoint('origin', event)} busy={busy} />
          </section>
          <section className="catalog-readonly" aria-label="现有目录">
            <div><h3>现有 Target</h3>{loadingData ? <p>读取中…</p> : targets.length ? <PointDirectory kind="target" points={targets} editingPoint={editingPoint} setEditingPoint={setEditingPoint} editError={pointEditErrors} busy={busy} onSave={editPoint} onAction={pointAction} /> : loadError ? <p>目录读取不完整，请重试；当前无法确认是否为空。</p> : <p>暂无 Target。</p>}</div>
            <div><h3>现有 Origin</h3>{loadingData ? <p>读取中…</p> : origins.length ? <PointDirectory kind="origin" points={origins} editingPoint={editingPoint} setEditingPoint={setEditingPoint} editError={pointEditErrors} busy={busy} onSave={editPoint} onAction={pointAction} /> : loadError ? <p>目录读取不完整，请重试；当前无法确认是否为空。</p> : <p>暂无 Origin。</p>}</div>
          </section>
          <div className="catalog-options">
            <label><input type="checkbox" checked={includeArchived} disabled={busy || loadingData} onChange={(event) => setIncludeArchived(event.target.checked)} />显示已归档</label>
            <span>列表会重新读取并保留当前点位状态。</span>
          </div>
          <section className="admin-section map-preview-section" aria-labelledby="map-preview-title">
            <div className="section-heading admin-section-heading"><div><p className="eyebrow">POINT REVIEW</p><h2 id="map-preview-title">地图预览。</h2></div><span className="status-pill">{mapCatalog ? mapCatalog.asset_status.toUpperCase() : mapCatalogError ? '读取失败' : '状态待读取'}</span></div>
            <p className="form-note">选择 Target 后点击正式 Mirage 地图预览位置，再明确保存目标位置。Origin 与 Lineup 不在地图上编辑或绘制。</p>
            {mapCatalogError && <p className="inline-error" role="alert">{mapCatalogError} <button className="text-button inline-retry" type="button" onClick={() => loadMapCatalog()}>重新读取地图状态</button></p>}
            <TargetPositionEditor
              targets={targets}
              lineups={lineups}
              lineupsLoaded={lineupsLoaded}
              map={mapCatalog}
              busy={busy}
              onSave={async (id, position) => {
                const saved = await call(() => adminApi.positionTarget(authorization, id, position));
                if (saved) await loadPointDirectory('target');
                return saved;
              }}
            />
          </section>
        </section>}

        {activeTab === 'lineups' && <section id="admin-panel-lineups" role="tabpanel" aria-labelledby="admin-tab-lineups">
          <section className="admin-section" aria-labelledby="lineup-create-title">
            <div className="section-heading admin-section-heading"><div><p className="eyebrow">02 · LINEUP DRAFT</p><h2 id="lineup-create-title">创建路线草稿。</h2></div></div>
            <form className="admin-form lineup-create-form" onSubmit={createLineup}>
              <div className="form-grid two-columns">
                <SelectField id="lineup-target" label="Target" value={lineupForm.targetId} onChange={(value) => setLineupForm((old) => ({ ...old, targetId: value }))} options={targets.filter((item) => item.is_active).map((item) => ({ value: item.id, label: `${item.name_zh} · ${item.slug}` }))} disabled={busy} />
                <SelectField id="lineup-origin" label="Origin" value={lineupForm.originId} onChange={(value) => setLineupForm((old) => ({ ...old, originId: value }))} options={origins.filter((item) => item.is_active).map((item) => ({ value: item.id, label: `${item.name_zh} · ${item.slug}` }))} disabled={busy} />
              </div>
              <StepFields prefix="new-lineup" values={{ setup: lineupForm.setup, aim: lineupForm.aim, throw: lineupForm.throwText, result: lineupForm.result }} onChange={(field, value) => setLineupForm((old) => ({ ...old, [field === 'throw' ? 'throwText' : field]: value }))} disabled={busy} />
              <label htmlFor="lineup-sort-order">排序</label><input id="lineup-sort-order" type="number" min="0" max="9999" value={lineupForm.sortOrder} disabled={busy} onChange={(event) => setLineupForm((old) => ({ ...old, sortOrder: event.target.value }))} />
              {lineupError && <p className="inline-error" role="alert">{lineupError}</p>}
              <button className="button button-primary" type="submit" disabled={busy || !targets.length || !origins.length}>保存草稿</button>
            </form>
          </section>

          <section className="admin-section" aria-labelledby="lineup-list-title">
            <div className="section-heading admin-section-heading"><div><p className="eyebrow">03 · REVIEW & PUBLISH</p><h2 id="lineup-list-title">路线工作台。</h2></div><button className="button button-secondary" type="button" onClick={() => void loadAll()}>重新读取</button></div>
            {loadingData && <div className="empty-card">正在读取目录与路线…</div>}
            {!loadingData && !lineups.length && (loadError ? <div className="empty-card">路线列表读取不完整，请重试；当前无法确认是否为空。</div> : <div className="empty-card">还没有路线草稿。</div>)}
            <div className="admin-lineup-list">
              {lineups.map((lineup) => (
                <LineupEditor
                  key={lineup.id}
                  lineup={lineup}
                  editing={editingId === lineup.id}
                  editingSteps={editingId === lineup.id ? editingSteps : null}
                  editingSortOrder={editingId === lineup.id ? editingSortOrder : null}
                  editingCheckInfo={editingId === lineup.id ? editingCheckInfo : null}
                  onEdit={() => { setEditingId(lineup.id); setEditingSteps(lineup.steps); setEditingSortOrder(String(lineup.sort_order)); setEditingCheckInfo(checkInfoForm(lineup.check_info)); }}
                  onCancel={() => { setEditingId(null); setEditingSteps(null); setEditingSortOrder(null); setEditingCheckInfo(null); }}
                  onChange={setEditingSteps}
                  onSortOrderChange={setEditingSortOrder}
                  onCheckInfoChange={setEditingCheckInfo}
                  onSaveTeaching={() => void saveTeaching(lineup.id)}
                  onSaveCheckInfo={() => void saveCheckInfo(lineup.id)}
                  pending={busyLineupId === lineup.id}
                  disabled={busyLineupId !== null}
                  editError={lineupErrors[lineup.id] ?? ''}
                  onUpload={() => void upload(lineup)}
                  file={uploadFiles[lineup.id]}
                  rights={uploadRights[lineup.id] ?? ''}
                  uploadError={uploadErrors[lineup.id] ?? ''}
                  inputVersion={uploadInputVersion[lineup.id] ?? 0}
                  onFile={(file) => setUploadFiles((old) => ({ ...old, [lineup.id]: file }))}
                  onRights={(rights) => setUploadRights((old) => ({ ...old, [lineup.id]: rights }))}
                  publishChoice={publishChoice[lineup.id]}
                  publishError={publishErrors[lineup.id] ?? ''}
                  onPublishChoice={(value) => setPublishChoice((old) => ({ ...old, [lineup.id]: value }))}
                  onPublish={() => void publish(lineup)}
                  priority={priorityDrafts[lineup.id] ?? String(lineup.sort_order)}
                  onPriorityChange={(value) => setPriorityDrafts((old) => ({ ...old, [lineup.id]: value }))}
                  onPrioritySave={() => void savePriority(lineup)}
                  onAction={(action) => void lineupAction(lineup, action)}
                />
              ))}
            </div>
          </section>

          <section className="admin-section" aria-labelledby="batch-import-title">
            <div className="section-heading admin-section-heading"><div><p className="eyebrow">04 · BATCH DRAFTS</p><h2 id="batch-import-title">按清单导入草稿。</h2></div></div>
            <div className="admin-form batch-import-form">
              <p className="form-note">只处理清单中 action=create_upload_publish 的路线。操作可安全重试；已有完全一致且视频 ready 的草稿会跳过。这里不会发布路线，也不会修改 Target 地图坐标。</p>
              <label htmlFor="batch-manifest">JSON 内容清单</label>
              <input id="batch-manifest" key={`manifest-${batchInputVersion}`} type="file" accept="application/json,.json" disabled={busy || busyLineupId !== null} onChange={(event) => setBatchManifest(event.target.files?.[0])} />
              <label htmlFor="batch-videos">清单内全部素材视频</label>
              <input id="batch-videos" key={`videos-${batchInputVersion}`} type="file" accept="video/mp4,video/webm,video/quicktime" multiple disabled={busy || busyLineupId !== null} onChange={(event) => setBatchVideos(Array.from(event.target.files ?? []))} />
              <label htmlFor="batch-rights">整批 rights_note</label>
              <textarea id="batch-rights" disabled={busy || busyLineupId !== null} value={batchRights} onChange={(event) => setBatchRights(event.target.value)} />
              <p className="form-note">已选择：{batchManifest?.name ?? '未选清单'}；{batchVideos.length} 个视频。</p>
              {batchError && <p className="inline-error" role="alert">{batchError}</p>}
              {batchStatus && <p className="batch-status" role="status" aria-live="polite">{batchStatus}</p>}
              <button className="button button-secondary" type="button" disabled={busy || busyLineupId !== null || !batchManifest || !batchVideos.length} onClick={() => void importBatch()}>{busyLineupId === '__batch_import__' ? '逐条导入中…' : '导入为视频就绪草稿'}</button>
            </div>
          </section>
        </section>}
        {activeTab === 'metrics' && <section id="admin-panel-metrics" role="tabpanel" aria-labelledby="admin-tab-metrics"><MetricsPanel metrics={metrics ?? emptyMetrics} loading={metricsLoading} error={metricsError} onRetry={loadMetrics} /></section>}
      </main>
    </div>
  );
}

function PointForm({ title, prefix, form, setForm, onSubmit, busy }: { title: string; prefix: string; form: PointFormState; setForm: React.Dispatch<React.SetStateAction<PointFormState>>; onSubmit: (event: FormEvent<HTMLFormElement>) => void; busy: boolean }) {
  const update = (key: keyof PointFormState, value: string) => setForm((old) => ({ ...old, [key]: value }));
  return (
    <form className="admin-form compact-admin-form" onSubmit={onSubmit}>
      <h3>{title}</h3>
      <p className="form-note">Target 创建后在下方正式 Mirage 地图编辑器中定位；Origin 不提供地图位置。</p>
      <label htmlFor={`${prefix}-slug`}>slug（必填）</label><input id={`${prefix}-slug`} name={`${prefix}-slug`} required disabled={busy} value={form.slug} onChange={(event) => update('slug', event.target.value)} />
      <label htmlFor={`${prefix}-name-zh`}>中文名称（必填）</label><input id={`${prefix}-name-zh`} name={`${prefix}-name-zh`} required disabled={busy} value={form.nameZh} onChange={(event) => update('nameZh', event.target.value)} />
      <label htmlFor={`${prefix}-name-en`}>英文名称（可选）</label><input id={`${prefix}-name-en`} name={`${prefix}-name-en`} disabled={busy} value={form.nameEn} onChange={(event) => update('nameEn', event.target.value)} />
      <label htmlFor={`${prefix}-description`}>说明</label><textarea id={`${prefix}-description`} disabled={busy} value={form.description} onChange={(event) => update('description', event.target.value)} />
      <label htmlFor={`${prefix}-sort-order`}>排序</label><input id={`${prefix}-sort-order`} type="number" min="0" max="9999" disabled={busy} value={form.sortOrder} onChange={(event) => update('sortOrder', event.target.value)} />
      <label htmlFor={`${prefix}-aliases`}>别名（逗号分隔，可选）</label><input id={`${prefix}-aliases`} disabled={busy} value={form.aliases} onChange={(event) => update('aliases', event.target.value)} />
      <button className="button button-secondary" type="submit" disabled={busy}>{busy ? '保存中…' : title}</button>
    </form>
  );
}

function pointEditState(point: PointOut): PointFormState {
  return {
    slug: point.slug,
    nameZh: point.name_zh,
    nameEn: point.name_en ?? '',
    description: point.description,
    sortOrder: String(point.sort_order),
    aliases: point.aliases.join('，'),
  };
}

export function svgClientPointToPercent(
  svg: SVGSVGElement,
  clientX: number,
  clientY: number,
): TargetPositionPayload | null {
  const ctm = svg.getScreenCTM();
  if (!ctm || typeof DOMPoint === 'undefined') return null;
  const point = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
  if (!Number.isFinite(point.x) || !Number.isFinite(point.y) || point.x < 0 || point.x > 1000 || point.y < 0 || point.y > 1000) return null;
  return { map_x: Number((point.x / 10).toFixed(2)), map_y: Number((point.y / 10).toFixed(2)) };
}

function TargetPositionEditor({
  targets,
  lineups,
  lineupsLoaded,
  map,
  busy,
  onSave,
}: {
  targets: PointOut[];
  lineups: AdminLineupOut[];
  lineupsLoaded: boolean;
  map: MapResponse['map'] | null;
  busy: boolean;
  onSave: (id: string, position: TargetPositionPayload) => Promise<PointOut | null>;
}) {
  const activeTargets = targets.filter((target) => target.is_active);
  const publishedTargetIds = new Set(lineups.filter((lineup) => lineup.status === 'published').map((lineup) => lineup.target.id));
  const publicTargets = targets.filter((target) => target.is_active && publishedTargetIds.has(target.id) && target.map_x !== null && target.map_y !== null);
  const [selectedId, setSelectedId] = useState(activeTargets[0]?.id ?? '');
  const [previewTargetId, setPreviewTargetId] = useState<string | null>(null);
  const [preview, setPreview] = useState<TargetPositionPayload | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const effectiveSelectedId = activeTargets.some((target) => target.id === selectedId) ? selectedId : activeTargets[0]?.id ?? '';
  const selected = activeTargets.find((target) => target.id === effectiveSelectedId) ?? null;
  const clearAllowed = lineupsLoaded && selected !== null && !publishedTargetIds.has(selected.id);
  const selectedPreview = previewTargetId === selected?.id
    ? preview
    : selected && selected.map_x !== null && selected.map_y !== null
      ? { map_x: selected.map_x, map_y: selected.map_y }
      : null;
  const previewReady = selectedPreview !== null && selectedPreview.map_x !== null && selectedPreview.map_y !== null;

  function selectTarget(id: string): void {
    setSelectedId(id);
    const target = activeTargets.find((item) => item.id === id);
    setPreviewTargetId(id);
    setPreview(target && target.map_x !== null && target.map_y !== null
      ? { map_x: target.map_x, map_y: target.map_y }
      : null);
    setError('');
  }

  function clickMap(event: ReactMouseEvent<SVGSVGElement>): void {
    if (!selected) return;
    const position = svgClientPointToPercent(event.currentTarget, event.clientX, event.clientY);
    if (!position) {
      setError('无法把当前点击转换为 Mirage 地图坐标，请重试。');
      return;
    }
    setPreviewTargetId(selected.id);
    setPreview(position);
    setError('');
  }

  async function save(position: TargetPositionPayload | null = selectedPreview): Promise<void> {
    if (!selected || saving || busy || (position === null && !clearAllowed)) return;
    setSaving(true);
    setError('');
    try {
      await onSave(selected.id, position ?? { map_x: null, map_y: null });
      setPreviewTargetId(null);
      setPreview(null);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setSaving(false);
    }
  }

  return <div className="target-position-editor">
    <div className="target-map-controls">
      <label htmlFor="target-position-select">选择 Target</label>
      <select id="target-position-select" value={effectiveSelectedId} disabled={busy || saving || !activeTargets.length} onChange={(event) => selectTarget(event.target.value)}>
        <option value="">请选择 Target</option>
        {activeTargets.map((target) => <option key={target.id} value={target.id}>{target.name_zh} · {target.slug}</option>)}
      </select>
      {selected && <span className="form-note">当前：{coordinateLabel(selected)}</span>}
    </div>
    <div className="target-map-editor" aria-label="Mirage Target 位置编辑器">
      <svg ref={svgRef} className="target-map-svg" viewBox="0 0 1000 1000" role="img" aria-label="Mirage 正式地图，点击设置 Target 预览位置" onClick={clickMap}>
        <rect className="target-map-hit-area" x="0" y="0" width="1000" height="1000" fill="transparent" pointerEvents="all" aria-hidden="true" />
        <image href={map?.asset_url ?? '/assets/maps/mirage.svg'} x="0" y="0" width="1000" height="1000" preserveAspectRatio="xMidYMid meet" aria-hidden="true" />
        {publicTargets.map((target) => <g key={target.id} className="target-map-marker" role="img" aria-label={`公开 Target ${target.name_zh}`}>
          <circle cx={target.map_x! * 10} cy={target.map_y! * 10} r="14" />
        </g>)}
        {selected && previewReady && <g className="target-map-preview" aria-label={`${selected.name_zh} 预览位置`}>
          <circle cx={selectedPreview.map_x! * 10} cy={selectedPreview.map_y! * 10} r="22" />
          <circle cx={selectedPreview.map_x! * 10} cy={selectedPreview.map_y! * 10} r="7" />
        </g>}
      </svg>
    </div>
    {!activeTargets.length && <p className="empty-card">暂无可定位的 active Target。</p>}
    {selected && <p className="form-note">{previewReady ? `预览坐标：${selectedPreview.map_x!.toFixed(2)}, ${selectedPreview.map_y!.toFixed(2)}` : '预览坐标：待定位'}</p>}
    <div className="inline-actions">
      <button className="button button-primary" type="button" disabled={!selected || !previewReady || busy || saving} onClick={() => void save()}>{saving ? '保存中…' : '保存目标位置'}</button>
      {clearAllowed && <button className="button button-secondary" type="button" disabled={!selected || busy || saving} onClick={() => void save(null)}>清除定位</button>}
    </div>
    {error && <p className="inline-error" role="alert">{error}</p>}
  </div>;
}

function PointDirectory({
  kind,
  points,
  editingPoint,
  setEditingPoint,
  editError,
  busy,
  onSave,
  onAction,
}: {
  kind: PointKind;
  points: PointOut[];
  editingPoint: { kind: PointKind; id: string } | null;
  setEditingPoint: (value: { kind: PointKind; id: string } | null) => void;
  editError: Record<string, string>;
  busy: boolean;
  onSave: (kind: PointKind, point: PointOut, form: PointFormState) => Promise<void>;
  onAction: (kind: PointKind, point: PointOut, archive: boolean) => Promise<void>;
}) {
  return <ul>{points.map((point) => <li key={point.id} className="point-directory-item">
    {editingPoint?.kind === kind && editingPoint.id === point.id
      ? <PointEditor point={point} kind={kind} busy={busy} error={editError[point.id] ?? ''} onCancel={() => setEditingPoint(null)} onSave={onSave} />
      : <>
        <div className="point-summary"><strong>{point.name_zh}</strong><span>{point.slug}</span>{kind === 'target' && <span>{coordinateLabel(point)}</span>}<span>排序 {point.sort_order}</span><span>{point.aliases.length ? `别名：${point.aliases.join('、')}` : '无别名'}</span><small>{point.description || '暂无说明'} · {point.is_active ? '启用中' : '已归档'}</small></div>
        <div className="point-actions"><button className="text-button" type="button" disabled={busy} onClick={() => setEditingPoint({ kind, id: point.id })}>编辑</button>{point.is_active ? <button className="text-button" type="button" disabled={busy} onClick={() => void onAction(kind, point, true)}>归档</button> : <button className="text-button" type="button" disabled={busy} onClick={() => void onAction(kind, point, false)}>恢复</button>}</div>
        {editError[point.id] && <p className="inline-error" role="alert">{editError[point.id]}</p>}
      </>}
  </li>)}</ul>;
}

function PointEditor({
  point,
  kind,
  busy,
  error,
  onCancel,
  onSave,
}: {
  point: PointOut;
  kind: PointKind;
  busy: boolean;
  error: string;
  onCancel: () => void;
  onSave: (kind: PointKind, point: PointOut, form: PointFormState) => Promise<void>;
}) {
  const [form, setForm] = useState(() => pointEditState(point));
  function update(key: keyof PointFormState, value: string) {
    setForm((old) => ({ ...old, [key]: value }));
  }
  return <div className="point-editor admin-form">
    <strong>{kind === 'target' ? '编辑 Target' : '编辑 Origin'} · {point.slug}</strong>
    <label htmlFor={`edit-point-name-${point.id}`}>中文名称</label><input id={`edit-point-name-${point.id}`} value={form.nameZh} disabled={busy} onChange={(event) => update('nameZh', event.target.value)} />
    <label htmlFor={`edit-point-name-en-${point.id}`}>英文名称</label><input id={`edit-point-name-en-${point.id}`} value={form.nameEn} disabled={busy} onChange={(event) => update('nameEn', event.target.value)} />
    <label htmlFor={`edit-point-description-${point.id}`}>说明</label><textarea id={`edit-point-description-${point.id}`} value={form.description} disabled={busy} onChange={(event) => update('description', event.target.value)} />
    <label htmlFor={`edit-point-sort-${point.id}`}>排序</label><input id={`edit-point-sort-${point.id}`} type="number" min="0" max="9999" value={form.sortOrder} disabled={busy} onChange={(event) => update('sortOrder', event.target.value)} />
    <label htmlFor={`edit-point-aliases-${point.id}`}>别名</label><input id={`edit-point-aliases-${point.id}`} value={form.aliases} disabled={busy} onChange={(event) => update('aliases', event.target.value)} />
    {error && <p className="inline-error" role="alert">{error}</p>}
    <div className="inline-actions"><button className="button button-primary" type="button" disabled={busy} onClick={() => void onSave(kind, point, form)}>保存点位</button><button className="button button-secondary" type="button" disabled={busy} onClick={onCancel}>取消</button></div>
  </div>;
}

function MetricsPanel({ metrics, loading, error, onRetry }: { metrics: MetricsSummary; loading: boolean; error: string; onRetry: () => void }) {
  const counts = metrics.response_counts ?? emptyMetrics.response_counts;
  const provider = metrics.provider_usage ?? emptyMetrics.provider_usage;
  return <section className="admin-section metrics-section" aria-labelledby="metrics-title">
    <div className="section-heading admin-section-heading"><div><p className="eyebrow">06 · OPERATIONS</p><h2 id="metrics-title">指标摘要。</h2></div><span className="status-pill">近 24 小时</span></div>
    {loading ? <div className="empty-card">正在读取指标…</div> : error ? <div className="empty-card"><p className="inline-error" role="alert">{error}</p><button className="button button-secondary" type="button" onClick={onRetry}>重新读取</button></div> : <>
      <div className="metrics-grid">{Object.entries(counts).map(([key, value]) => <div className="metric-card" key={key}><span>{({ match: '直接命中', route_matches: '路线列表', clarification: '澄清', not_found: '未找到', out_of_scope: '超范围' } as Record<string, string>)[key] ?? key}</span><strong>{value}</strong></div>)}</div>
      <dl className="metrics-facts"><div><dt>统计窗口</dt><dd>{metricTime(metrics.from)} – {metricTime(metrics.to)}</dd></div><div><dt>延迟 P50</dt><dd>{metrics.latency_ms?.p50 ?? 0} ms</dd></div><div><dt>延迟 P95</dt><dd>{metrics.latency_ms?.p95 ?? 0} ms</dd></div></dl>
      <div className="metrics-provider-grid" aria-label="Provider 使用明细">
        <ProviderMetric title="Intent" metric={provider.intent} tokenFields />
        <ProviderMetric title="ASR" metric={provider.asr} />
      </div>
      <div className="metrics-lists"><div><h3>未找到组合</h3>{(metrics.not_found_combinations ?? []).length ? <ul>{(metrics.not_found_combinations ?? []).map((item) => <li key={`${item.target_slug}-${item.origin_slug ?? 'none'}`}>{item.target_slug} / {item.origin_slug ?? '未指定起点'} · {item.count}</li>)}</ul> : <p>无记录</p>}</div><div><h3>错误</h3>{(metrics.errors ?? []).length ? <ul>{(metrics.errors ?? []).map((item) => <li key={item.code}>{item.code} · {item.count}</li>)}</ul> : <p>无记录</p>}</div></div>
    </>}
  </section>;
}

function ProviderMetric({ title, metric, tokenFields = false }: { title: string; metric: MetricsSummary['provider_usage']['intent'] | MetricsSummary['provider_usage']['asr']; tokenFields?: boolean }) {
  return <section className="provider-metric" aria-label={`${title} 使用明细`}><h3>{title}</h3><dl><div><dt>attempts</dt><dd>{metric.attempts}</dd></div><div><dt>success</dt><dd>{metric.success}</dd></div><div><dt>failed</dt><dd>{metric.failed}</dd></div><div><dt>unknown</dt><dd>{metric.unknown}</dd></div>{tokenFields && 'input_tokens' in metric && <><div><dt>input_tokens</dt><dd>{metric.input_tokens}</dd></div><div><dt>output_tokens</dt><dd>{metric.output_tokens}</dd></div></>}{!tokenFields && 'reserved_seconds' in metric && <div><dt>reserved_seconds</dt><dd>{metric.reserved_seconds}</dd></div>}</dl></section>;
}

function SelectField({ id, label, value, onChange, options, disabled }: { id: string; label: string; value: string; onChange: (value: string) => void; options: Array<{ value: string; label: string }>; disabled?: boolean }) {
  return <div><label htmlFor={id}>{label}</label><select id={id} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}><option value="">请选择</option>{options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div>;
}

function StepFields({ prefix, values, onChange, disabled }: { prefix: string; values: { setup: string; aim: string; throw: string; result: string }; onChange: (field: keyof typeof values, value: string) => void; disabled?: boolean }) {
  return <div className="steps-form"><label htmlFor={`${prefix}-setup`}>站位步骤</label><textarea id={`${prefix}-setup`} disabled={disabled} value={values.setup} onChange={(event) => onChange('setup', event.target.value)} /><label htmlFor={`${prefix}-aim`}>瞄点步骤</label><textarea id={`${prefix}-aim`} disabled={disabled} value={values.aim} onChange={(event) => onChange('aim', event.target.value)} /><label htmlFor={`${prefix}-throw`}>投掷步骤</label><textarea id={`${prefix}-throw`} disabled={disabled} value={values.throw} onChange={(event) => onChange('throw', event.target.value)} /><label htmlFor={`${prefix}-result`}>结果步骤</label><textarea id={`${prefix}-result`} disabled={disabled} value={values.result} onChange={(event) => onChange('result', event.target.value)} /></div>;
}

interface LineupEditorProps {
  lineup: AdminLineupOut;
  editing: boolean;
  editingSteps: AdminLineupOut['steps'] | null;
  editingSortOrder: string | null;
  editingCheckInfo: CheckInfoFormState | null;
  onEdit: () => void;
  onCancel: () => void;
  onChange: (steps: AdminLineupOut['steps'] | null) => void;
  onSortOrderChange: (value: string | null) => void;
  onCheckInfoChange: (value: CheckInfoFormState | null) => void;
  onSaveTeaching: () => void;
  onSaveCheckInfo: () => void;
  pending: boolean;
  disabled: boolean;
  editError: string;
  onUpload: () => void;
  file: File | undefined;
  rights: string;
  uploadError: string;
  inputVersion: number;
  onFile: (file: File | undefined) => void;
  onRights: (rights: string) => void;
  publishChoice: boolean | undefined;
  publishError: string;
  onPublishChoice: (value: boolean) => void;
  onPublish: () => void;
  priority: string;
  onPriorityChange: (value: string) => void;
  onPrioritySave: () => void;
  onAction: (action: 'setDefault' | 'unpublish' | 'markNeeds' | 'archive' | 'restore' | 'archiveVideo' | 'restoreVideo') => void;
}

function LineupEditor(props: LineupEditorProps) {
  const { lineup } = props;
  const video = lineup.primary_video;
  const editable = lineup.status === 'draft' || lineup.status === 'needs_revalidation';
  return (
    <article className={`admin-lineup-card lineup-${lineup.status}`}>
      <div className="lineup-card-heading"><div><p className="eyebrow">{statusLabel(lineup.status)} · ID {lineup.id} · 优先级 {lineup.sort_order}</p><h3>{lineup.origin.name_zh} → {lineup.target.name_zh}</h3></div><span className="status-pill">{lineup.is_default ? 'DEFAULT' : '非默认'}</span></div>
      <div className="lineup-step-summary"><span><b>站位</b>{lineup.steps.setup || '—'}</span><span><b>瞄点</b>{lineup.steps.aim || '—'}</span><span><b>投掷</b>{lineup.steps.throw || '—'}</span><span><b>结果</b>{lineup.steps.result || '—'}</span></div>
      {video?.status === 'ready' && video.poster_url && <div className="saved-video-summary"><img className="admin-video-poster" src={video.poster_url} alt={`${lineup.origin.name_zh} 到 ${lineup.target.name_zh} 的已保存视频封面`} /><div><strong>主视频已就绪</strong>{video.rights_note && <p className="video-state">授权说明：{video.rights_note}</p>}</div></div>}
      {editable && props.editing && props.editingSteps && props.editingSortOrder !== null && props.editingCheckInfo && <div className="draft-edit"><StepFields prefix={`edit-${lineup.id}`} values={props.editingSteps} disabled={props.disabled} onChange={(field, value) => props.onChange({ ...props.editingSteps!, [field]: value })} /><label htmlFor={`edit-${lineup.id}-sort-order`}>优先级</label><input id={`edit-${lineup.id}-sort-order`} type="number" min="0" max="9999" step="1" disabled={props.disabled} value={props.editingSortOrder} onChange={(event) => props.onSortOrderChange(event.target.value)} /><button className="button button-primary" type="button" disabled={props.disabled} onClick={props.onSaveTeaching}>{props.pending ? '保存中…' : '保存教学内容与优先级'}</button><label htmlFor={`edit-${lineup.id}-checked-at`}>复查时间（可选）</label><input id={`edit-${lineup.id}-checked-at`} type="datetime-local" disabled={props.disabled} value={props.editingCheckInfo.lastCheckedAt} onChange={(event) => props.onCheckInfoChange({ ...props.editingCheckInfo!, lastCheckedAt: event.target.value })} /><label htmlFor={`edit-${lineup.id}-version-note`}>版本说明（可选）</label><input id={`edit-${lineup.id}-version-note`} disabled={props.disabled} value={props.editingCheckInfo.gameVersionNote} onChange={(event) => props.onCheckInfoChange({ ...props.editingCheckInfo!, gameVersionNote: event.target.value })} /><div className="inline-actions"><button className="button button-secondary" type="button" disabled={props.disabled} onClick={props.onSaveCheckInfo}>{props.pending ? '保存中…' : '保存检查信息'}</button><button className="button button-secondary" type="button" disabled={props.disabled} onClick={props.onCancel}>结束编辑</button></div>{props.editError && <p className="inline-error" role="alert">{props.editError}</p>}</div>}
      {editable && !props.editing && <button className="text-button" type="button" disabled={props.disabled} onClick={props.onEdit}>{lineup.status === 'needs_revalidation' ? '修复待复查路线' : '编辑草稿'}</button>}
      {editable && <div className="upload-box"><h4>主视频</h4><label htmlFor={`video-${lineup.id}`}>主视频文件</label><input id={`video-${lineup.id}`} key={props.inputVersion} type="file" accept="video/mp4,video/webm,video/quicktime" disabled={props.disabled} onChange={(event) => props.onFile(event.target.files?.[0])} /><label htmlFor={`rights-${lineup.id}`}>rights_note</label><textarea id={`rights-${lineup.id}`} disabled={props.disabled} value={props.rights} onChange={(event) => props.onRights(event.target.value)} placeholder="例如：自录视频，或注明授权来源与许可范围" /><button className="button button-secondary" type="button" disabled={props.disabled} onClick={props.onUpload}>{props.pending ? '处理中…' : '上传主视频'}</button>{props.uploadError && <p className="inline-error" role="alert">{props.uploadError}</p>}</div>}
      {video && <p className="video-state">视频状态：{video.status}{video.failure_reason ? ` · ${video.failure_reason}` : ''}</p>}
      {lineup.default_needs_review && <p className="inline-error" role="status">默认路线因媒体故障自动调整，请确认</p>}
      {props.editError && !props.editing && <p className="inline-error" role="alert">{props.editError}</p>}
      {editable && <div className="publish-box"><fieldset><legend>发布时的默认选择（必选）</legend><label><input type="radio" name={`default-${lineup.id}`} disabled={props.disabled} checked={props.publishChoice === true} onChange={() => props.onPublishChoice(true)} />设为默认（true）</label><label><input type="radio" name={`default-${lineup.id}`} disabled={props.disabled} checked={props.publishChoice === false} onChange={() => props.onPublishChoice(false)} />不设为默认（false）</label></fieldset><button className="button button-primary" type="button" disabled={props.disabled} onClick={props.onPublish}>{props.pending ? '发布中…' : '发布路线'}</button>{props.publishError && <p className="inline-error" role="alert">{props.publishError}</p>}</div>}
      {lineup.status === 'published' && <div className="priority-editor"><label htmlFor={`priority-${lineup.id}`}>优先级</label><input id={`priority-${lineup.id}`} type="number" min="0" max="9999" step="1" disabled={props.disabled} value={props.priority} onChange={(event) => props.onPriorityChange(event.target.value)} /><button className="button button-secondary" type="button" disabled={props.disabled} onClick={props.onPrioritySave}>{props.pending ? '保存中…' : '保存优先级'}</button></div>}
      <div className="lineup-lifecycle-actions">
        {lineup.status === 'published' && <><button className="button button-secondary" type="button" disabled={props.disabled} onClick={() => props.onAction('setDefault')}>设为默认</button><button className="button button-secondary" type="button" disabled={props.disabled} onClick={() => props.onAction('unpublish')}>下架</button><button className="button button-secondary" type="button" disabled={props.disabled} onClick={() => props.onAction('markNeeds')}>标记待复查</button></>}
        {lineup.status !== 'archived' && lineup.status !== 'published' && <button className="button button-secondary" type="button" disabled={props.disabled} onClick={() => props.onAction('archive')}>归档路线</button>}
        {lineup.status === 'archived' && <button className="button button-secondary" type="button" disabled={props.disabled} onClick={() => props.onAction('restore')}>恢复路线</button>}
        {video?.status === 'ready' && lineup.status !== 'published' && lineup.status !== 'archived' && <button className="button button-secondary" type="button" disabled={props.disabled} onClick={() => props.onAction('archiveVideo')}>归档主视频</button>}
        {video?.status === 'archived' && lineup.status !== 'archived' && <button className="button button-secondary" type="button" disabled={props.disabled} onClick={() => props.onAction('restoreVideo')}>恢复主视频</button>}
      </div>
      {lineup.status === 'published' && <><p className="readonly-note">已发布教学字段只读；请先下架后修改。</p><p className="readonly-note">当前可调整优先级、默认、下架或标记待复查。</p></>}
    </article>
  );
}
