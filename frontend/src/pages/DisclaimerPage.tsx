import WorkshopHeader from '../components/WorkshopHeader';
import { Link } from 'react-router-dom';
import useReadingPageScroll from './useReadingPageScroll';

export default function DisclaimerPage() {
  useReadingPageScroll();
  return (
    <div className="site-frame workshop-public">
      <WorkshopHeader />
      <main className="page-shell compact-page" aria-labelledby="disclaimer-title">
        <div className="legal-heading"><p className="eyebrow">NOTICE / 01</p>
        <h1 id="disclaimer-title">非官方声明</h1></div>
        <div className="legal-layout"><nav className="legal-index" aria-label="页面目录"><p className="eyebrow">PAGE CONTENTS</p><h2>页面目录</h2><a href="#legal-part-1">教学与练习</a><a href="#legal-part-2">游戏进程与辅助</a><a href="#legal-part-3">内容与更新</a><a href="#legal-part-4">非官方关系</a></nav>        <div className="policy-copy">
          <p id="legal-part-1">内容用于玩家查阅教学与练习，可在对局间隙打开。</p>
          <p id="legal-part-2">CS2 Workshop 不读取游戏进程，不提供实时瞄准辅助。</p>
          <p id="legal-part-3">“本站人工整理”不保证游戏内成功，游戏更新可能使路线失效；有检查日期或版本时仅作参考。</p>
          <p id="legal-part-4">CS2 Workshop 与 Valve、Steam、Counter-Strike 2 无官方隶属、赞助或背书关系。</p>
        </div></div>
        <p className="legal-callout">
          本项目为非官方玩家教学工具，与 Valve 或 Counter-Strike 2 官方无隶属或赞助关系。
        </p>
        <Link className="back-link" to="/">
          ← 返回教学页
        </Link>
      </main>
      <footer className="site-footer">
        <span>CS2 Workshop · Disclaimer</span>
        <Link to="/privacy">隐私</Link>
      </footer>
    </div>
  );
}
