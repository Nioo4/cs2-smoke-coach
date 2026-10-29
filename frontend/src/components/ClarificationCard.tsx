import type { SearchClarification } from '../types/api';

export default function ClarificationCard({
  result,
  onSelect,
  onEdit,
  loading,
}: {
  result: SearchClarification;
  onSelect: (value: string) => void;
  onEdit: () => void;
  loading: boolean;
}) {
  return (
    <div className="drawer-body">
      <p className="drawer-intro">{result.question}</p>
      <div className="clarification-list">
        {result.options.map((option) => (
          <button className="choice-button" type="button" key={option.value} disabled={loading} onClick={() => onSelect(option.value)}>
            {option.label}
          </button>
        ))}
      </div>
      <button className="text-button" type="button" onClick={onEdit}>都不是，修改输入</button>
    </div>
  );
}
