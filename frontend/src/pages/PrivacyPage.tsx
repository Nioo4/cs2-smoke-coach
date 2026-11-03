import WorkshopHeader from '../components/WorkshopHeader';
import { Link } from 'react-router-dom';
import useReadingPageScroll from './useReadingPageScroll';

export default function PrivacyPage() {
  useReadingPageScroll();
  return (
    <div className="site-frame workshop-public">
      <WorkshopHeader />
      <main className="page-shell compact-page" aria-labelledby="privacy-title">
        <div className="legal-heading"><p className="eyebrow">POLICY / 01</p>
        <h1 id="privacy-title">隐私说明</h1></div>
        <div className="legal-layout"><nav className="legal-index" aria-label="页面目录"><p className="eyebrow">PAGE CONTENTS</p><h2>页面目录</h2><a href="#legal-part-1">账号与会话</a><a href="#legal-part-2">队伍与分享</a><a href="#legal-part-3">历史与保留</a><a href="#legal-part-4">变更审计</a><a href="#legal-part-5">文字查询</a><a href="#legal-part-6">教学视频</a><a href="#legal-part-7">语音转写</a><a href="#legal-part-8">匿名使用记录</a></nav>        <div className="policy-copy">
          <p id="legal-part-1">教学浏览不强制注册账号。使用战术本时，本站保存用户名、昵称、密码哈希与恢复码摘要；登录使用有效期 7 天的 HttpOnly 会话 Cookie。后台管理员凭据只在当前页面内存中保存，刷新或退出后清除。</p>
          <p id="legal-part-2">队伍成员可按授权查看战术正文及协作编辑位置。主动发布的分享是固定版本，持有分享链接的人可以查看已发布内容；若允许复制，复制后形成独立副本。</p>
          <p id="legal-part-3">战术历史保留 30 天，每份战术最多 100 份历史；归档的战术和队伍可在 30 天内恢复。活跃分享独立保留已发布快照。撤销分享后阻止新的读取与复制；已读取、下载或复制的内容无法收回。撤销的分享快照在 30 天后可由受控任务清理，相关备份可能额外保留最多 7 天。</p>
          <p id="legal-part-4">战术本中成功的账号、权限、内容和分享变更会记录操作者内部标识、资源标识、动作、结果及时间，用于追溯。这些审计记录不包含战术正文、密码、恢复码、会话令牌、分享或邀请令牌及光标信息。记录在在线数据库保留 30 天，由受控维护任务清理；备份可能额外保留最多 7 天。</p>
          <p id="legal-part-5">精确的本站目录查询不调用 AI；需要理解模糊中文表述时，会把完成本次请求所需的文字发送给阿里云百炼。首版不进行站外视频搜索。</p>
          <p id="legal-part-6">教学视频由本站自有或取得授权后上传；原始上传文件只在处理期间存放于临时目录。</p>
          <p id="legal-part-7">语音功能启用后，会把完成本次请求所需的短音频发送给阿里云百炼进行转写；临时音频会在请求结束后删除。</p>
          <p id="legal-part-8">公共检索不把原始搜索文本、短音频、Authorization 或 IP 写入数据库；只保存规范化位置字段和匿名使用记录。会话规范字段 30 分钟过期；匿名使用记录在在线数据库保留 30 天，备份最长保留 37 天。</p>
        </div></div>
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
      <span>CS2 Workshop · Privacy</span>
      <Link to="/disclaimer">非官方声明</Link>
    </footer>
  );
}
