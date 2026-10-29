import { Link } from 'react-router-dom';

export default function PrivacyPage() {
  return (
    <div className="site-frame">
      <header className="simple-header">
        <Link className="brand" to="/" aria-label="SmokeLine 首页">
          <span className="brand-mark" aria-hidden="true">
            SL
          </span>
          <span>SmokeLine</span>
        </Link>
        <span className="scope-pill">隐私说明</span>
      </header>
      <main className="page-shell compact-page" aria-labelledby="privacy-title">
        <p className="eyebrow">POLICY / 01</p>
        <h1 id="privacy-title">隐私说明</h1>
        <div className="policy-copy">
          <p>本站不要求玩家账号，不使用 Cookie；后台管理员凭据只在当前页面内存中保存，刷新或退出后清除。</p>
          <p>精确的本站目录查询不调用 AI；需要理解模糊中文表述时，会把完成本次请求所需的文字发送给阿里云百炼。首版不进行站外视频搜索。</p>
          <p>教学视频由本站自有或取得授权后上传；原始上传文件只在处理期间存放于临时目录。</p>
          <p>语音功能启用后，会把完成本次请求所需的短音频发送给阿里云百炼进行转写；临时音频会在请求结束后删除。</p>
          <p>公共检索不把原始搜索文本、短音频、Authorization 或 IP 写入数据库；只保存规范化位置字段和匿名使用记录。会话规范字段 30 分钟过期；匿名使用记录在在线数据库保留 30 天，备份最长保留 37 天。</p>
        </div>
        <Link className="back-link" to="/">
          ← 返回教学页
        </Link>
      </main>
      <PolicyFooter />
    </div>
  );
}

function PolicyFooter() {
  return (
    <footer className="site-footer">
      <span>SmokeLine · Privacy</span>
      <Link to="/disclaimer">非官方声明</Link>
    </footer>
  );
}
