import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import CasesPage from "./pages/CasesPage";
import HistoryPage from "./pages/HistoryPage";
import RunPage, { ActiveRunPage } from "./pages/RunPage";

export default function App() {
  const { pathname } = useLocation();
  const section = pathname === "/runs" || pathname.endsWith("/report") ? "history" : pathname.startsWith("/runs/") || pathname === "/execution" ? "execution" : "planning";
  return <div className="app-shell">
    <header className="app-nav"><Link className="brand" to="/"><span className="brand-mark">T</span>TestPilot</Link>
      <nav aria-label="主导航">{[{ key: "planning", to: "/", label: "规划" }, { key: "execution", to: "/execution", label: "执行" }, { key: "history", to: "/runs", label: "历史记录" }].map((entry) =>
        <Link key={entry.key} to={entry.to} className={section === entry.key ? "active" : ""} aria-current={section === entry.key ? "page" : undefined}>{entry.label}</Link>)}</nav>
      <span className="nav-caption">Android TV 主观测试</span>
    </header>
    <main><Routes>
      <Route path="/" element={<CasesPage />} />
      <Route path="/cases/:caseId/plan" element={<CasesPage />} />
      <Route path="/execution" element={<ActiveRunPage />} />
      <Route path="/runs" element={<HistoryPage />} />
      <Route path="/runs/:runId" element={<RunPage />} />
      <Route path="/runs/:runId/report" element={<HistoryPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes></main>
  </div>;
}
