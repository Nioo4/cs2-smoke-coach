export interface PointSummary {
  slug: string;
  name_zh: string;
  map_x: number | null;
  map_y: number | null;
}

export interface PrimaryVideo {
  url: string;
  poster_url: string;
  duration_ms: number;
}

export interface LineupSummary {
  id: string;
  is_default: boolean;
  variant_hint: string;
  primary_video: PrimaryVideo;
}

export interface OriginChoice {
  origin: PointSummary;
  lineup: LineupSummary;
}

export interface TargetChoices {
  target: PointSummary;
  origins: OriginChoice[];
}

export interface MapTarget extends PointSummary {
  origin_count: number;
}

export interface MapResponse {
  map: {
    slug: 'mirage';
    name_zh: string;
    asset_url: string;
    asset_status: 'placeholder' | 'approved';
  };
  published_lineup_count: number;
  targets: MapTarget[];
}

export interface TranscriptionResponse {
  text: string;
  duration_seconds: number;
}

export interface Notice {
  code: 'ORIGIN_UNRESOLVED';
  message: string;
}

export interface CheckInfo {
  last_checked_at: string | null;
  game_version_note: string | null;
}

export interface LineupDetail {
  id: string;
  target: PointSummary;
  origin: PointSummary;
  is_default: boolean;
  source: 'local_curated';
  steps: {
    setup: string;
    aim: string;
    throw: string;
    result: string;
  };
  check_info: CheckInfo;
  primary_video: PrimaryVideo;
  other_lineups: LineupSummary[];
}

export interface SearchMatch {
  type: 'match';
  conversation_id: string;
  source: 'local_curated';
  confidence: number;
  intent: {
    map: 'mirage';
    grenade: 'smoke';
    target_slug: string | null;
    origin_slug: string | null;
  };
  notices: Notice[];
  lineup: LineupDetail;
}

export interface RouteChoice {
  target: PointSummary;
  origin: PointSummary;
  lineup: LineupSummary;
}

export interface SearchRouteMatches {
  type: 'route_matches';
  conversation_id: string;
  source: 'local_curated';
  intent: SearchMatch['intent'];
  notices: Notice[];
  routes: RouteChoice[];
}

export interface SearchClarification {
  type: 'clarification';
  conversation_id: string;
  question: '你想封哪个目标位置？';
  field: 'target';
  options: Array<{ value: string; label: string }>;
}

export interface SearchNotFound {
  type: 'not_found';
  conversation_id: string;
  reason: 'NO_TARGET' | 'NO_LOCAL_RESULT';
  message: string;
}

export interface SearchOutOfScope {
  type: 'out_of_scope';
  conversation_id: string;
  message: '当前版本只支持 Mirage 的烟雾弹教学。';
  supported_scope: { maps: ['mirage']; grenades: ['smoke'] };
}

export type SearchRequest =
  | { kind: 'query'; text: string; conversation_id?: string | null; input_mode: 'text' | 'voice' }
  | { kind: 'selection'; conversation_id: string; selection: { field: 'target'; value: string } };

export type SearchResponse =
  | SearchMatch
  | SearchRouteMatches
  | SearchClarification
  | SearchNotFound
  | SearchOutOfScope;

export interface PointOut extends PointSummary {
  id: string;
  name_en: string | null;
  description: string;
  sort_order: number;
  aliases: string[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminVideoOut {
  id: string;
  status: string;
  duration_ms: number | null;
  size_bytes: number | null;
  rights_note: string;
  failure_reason: string | null;
  url: string | null;
  poster_url: string | null;
}

export interface AdminCatalogRef {
  id: string;
  slug: string;
  name_zh: string;
}

export interface AdminLineupOut {
  id: string;
  status: 'draft' | 'published' | 'needs_revalidation' | 'archived';
  is_default: boolean;
  default_needs_review: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
  target: AdminCatalogRef;
  origin: AdminCatalogRef;
  steps: {
    setup: string;
    aim: string;
    throw: string;
    result: string;
  };
  check_info: {
    last_checked_at: string | null;
    game_version_note: string | null;
  };
  primary_video: AdminVideoOut | null;
}

export interface PointCreatePayload {
  slug: string;
  name_zh: string;
  name_en?: string | null;
  description: string;
  sort_order: number;
  aliases: string[];
}

export interface PointPatchPayload {
  name_zh?: string;
  name_en?: string | null;
  description?: string;
  sort_order?: number;
  aliases?: string[];
}

export interface TargetPositionPayload {
  map_x: number | null;
  map_y: number | null;
}

export interface LineupCreatePayload {
  target_id: string;
  origin_id: string;
  steps: AdminLineupOut['steps'];
  sort_order: number;
  check_info: { last_checked_at: null; game_version_note: null };
}

export interface LineupPatchPayload {
  steps?: AdminLineupOut['steps'];
  sort_order?: number;
  check_info?: AdminLineupOut['check_info'];
}

export interface MetricsSummary {
  window_hours: 24;
  from: string;
  to: string;
  response_counts: {
    match: number;
    route_matches: number;
    clarification: number;
    not_found: number;
    out_of_scope: number;
  };
  latency_ms: { p50: number; p95: number };
  provider_usage: {
    intent: { attempts: number; success: number; failed: number; unknown: number; input_tokens: number; output_tokens: number };
    asr: { attempts: number; success: number; failed: number; unknown: number; reserved_seconds: number };
  };
  not_found_combinations: Array<{ target_slug: string; origin_slug: string | null; count: number }>;
  errors: Array<{ code: string; count: number }>;
}
