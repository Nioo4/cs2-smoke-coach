import type { Notice, SearchNotFound, SearchOutOfScope } from '../types/api';

export function NoticeList({ notices }: { notices: Notice[] }) {
  if (!notices.length) return null;
  return <div className="notice-list" role="status" aria-live="polite">{notices.map((notice) => <p key={notice.code}>{notice.message}</p>)}</div>;
}

export default function ErrorNotice({ result, onEdit }: { result: SearchNotFound | SearchOutOfScope; onEdit: () => void }) {
  return (
    <div className="drawer-body notice-panel" role="status" aria-live="polite">
      <span className="notice-mark" aria-hidden="true">!</span>
      <h3>{result.type === 'out_of_scope' ? '当前范围有限' : '还没有找到路线'}</h3>
      <p>{result.message}</p>
      <button className="button button-secondary" type="button" onClick={onEdit}>修改输入</button>
    </div>
  );
}
