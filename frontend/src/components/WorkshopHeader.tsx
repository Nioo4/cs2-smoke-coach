import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import '../styles/workshop.css';

export default function WorkshopHeader({ children }: { children?: ReactNode }) {
  return <header className="workshop-header">
    <NavLink className="workshop-brand" to="/" aria-label="CS2 Workshop 首页"><span className="workshop-mark" aria-hidden="true">CS2</span><span><strong>CS2 Workshop</strong><small>烟雾教学 · 个人与队伍战术本</small></span></NavLink>
    <nav aria-label="主要导航"><NavLink to="/" end>烟雾教学</NavLink><NavLink to="/tactics">个人战术本</NavLink><NavLink to="/teams">我的队伍</NavLink><NavLink to="/account">账户</NavLink></nav>
    {children && <div className="workshop-header-tools">{children}</div>}
  </header>;
}
