import type { MapResponse, MapTarget } from '../types/api';

type FocusTarget = HTMLElement | SVGElement;

function hasCoordinates(target: MapTarget): target is MapTarget & { map_x: number; map_y: number } {
  return typeof target.map_x === 'number'
    && Number.isFinite(target.map_x)
    && typeof target.map_y === 'number'
    && Number.isFinite(target.map_y);
}

function markerTextAnchor(x: number): 'start' | 'middle' | 'end' {
  if (x < 100) return 'start';
  if (x > 900) return 'end';
  return 'middle';
}

export default function MirageMap({
  map,
  selectedTarget,
  loading,
  onTarget,
}: {
  map: MapResponse;
  selectedTarget: string | null;
  loading: boolean;
  onTarget: (slug: string, element: FocusTarget) => void;
}) {
  const positionedTargets = map.targets.filter(hasCoordinates);
  return <section className="manual-section map-section" aria-labelledby="map-title">
    <div className="section-heading"><div><p className="eyebrow">02 · MAP NAVIGATION</p><h2 id="map-title">在地图上选择。</h2></div><p className="section-note">选择 Target 查看可用路线</p></div>
    <div className="public-map-layout">
      <div className="public-map-canvas">
        <svg className="public-map-svg" viewBox="0 0 1000 1000" role="group" aria-label="Mirage 地图与 Target 标记">
          <image href={map.map.asset_url} x="0" y="0" width="1000" height="1000" preserveAspectRatio="xMidYMid meet" />
          {positionedTargets.map((item) => { const x = item.map_x * 10; const y = item.map_y * 10; return <g key={`map-target-${item.slug}`} className={`map-button map-target-button${selectedTarget === item.slug ? ' is-selected' : ''}`} role="button" tabIndex={0} aria-label={`Target ${item.name_zh}，${item.origin_count} 个可用起点`} aria-current={selectedTarget === item.slug ? 'true' : undefined} onClick={(event) => onTarget(item.slug, event.currentTarget)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onTarget(item.slug, event.currentTarget); } }}><circle className="map-hit-area" cx={x} cy={y} r="84" /><circle className="map-selection-ring" cx={x} cy={y} r="31" /><circle className="map-visible-marker" cx={x} cy={y} r="22" /><text x={x} y={y + 42} textAnchor={markerTextAnchor(x)}>{item.name_zh}</text></g>; })}
        </svg>
      </div>
      <div className="map-guide"><strong>{selectedTarget ? `${map.targets.find((item) => item.slug === selectedTarget)?.name_zh ?? 'Target'} · 已选中` : '选择一个 Target'}</strong><div className="map-target-list">{map.targets.map((item) => <button className={`map-target-card${selectedTarget === item.slug ? ' is-selected' : ''}`} type="button" key={item.slug} aria-current={selectedTarget === item.slug ? 'true' : undefined} disabled={loading} onClick={(event) => onTarget(item.slug, event.currentTarget)}><span>{item.name_zh}</span><small>{item.origin_count} 个可用起点</small></button>)}</div><p>地图只显示有公开教学的 Target；视频仍需你主动播放。</p></div>
    </div>
  </section>;
}
