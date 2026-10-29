import { Link } from 'react-router-dom';

export default function DisclaimerPage() {
  return (
    <div className="site-frame">
      <header className="simple-header">
        <Link className="brand" to="/" aria-label="SmokeLine 首页">
          <span className="brand-mark" aria-hidden="true">
            SL
          </span>
          <span>SmokeLine</span>
        </Link>
        <span className="scope-pill">非官方声明</span>
      </header>
      <main className="page-shell compact-page" aria-labelledby="disclaimer-title">
        <p className="eyebrow">NOTICE / 01</p>
        <h1 id="disclaimer-title">非官方声明</h1>
        <div className="policy-copy">
          <p>内容用于玩家查阅教学与练习，可在对局间隙打开。</p>
          <p>SmokeLine 不读取游戏进程，不提供实时瞄准辅助。</p>
          <p>“本站人工整理”不保证游戏内成功，游戏更新可能使路线失效；有检查日期或版本时仅作参考。</p>
          <p>SmokeLine 与 Valve、Steam、Counter-Strike 2 无官方隶属、赞助或背书关系。</p>
        </div>
        <p className="legal-callout">
          本项目为非官方玩家教学工具，与 Valve 或 Counter-Strike 2 官方无隶属或赞助关系。
        </p>
        <Link className="back-link" to="/">
          ← 返回教学页
        </Link>
      </main>
      <footer className="site-footer">
        <span>SmokeLine · Disclaimer</span>
        <Link to="/privacy">隐私</Link>
      </footer>
    </div>
  );
}
