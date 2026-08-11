import { ExperimentOutlined, HistoryOutlined } from "@ant-design/icons";
import { Layout, Menu, Typography } from "antd";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import CasesPage from "./pages/CasesPage";
import PlanPage from "./pages/PlanPage";
import ReportPage from "./pages/ReportPage";
import RunPage from "./pages/RunPage";

export default function App() {
  const location = useLocation();
  return (
    <Layout className="app-shell">
      <Layout.Header className="app-header">
        <Link to="/" className="brand"><ExperimentOutlined /> TV Test Agent</Link>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[location.pathname.startsWith("/runs") ? "runs" : "cases"]}
          items={[
            { key: "cases", icon: <ExperimentOutlined />, label: <Link to="/">用例</Link> },
            { key: "runs", icon: <HistoryOutlined />, label: <Link to="/runs">运行</Link> }
          ]}
        />
      </Layout.Header>
      <Layout.Content className="app-content">
        <Typography.Title level={2} className="page-title">Android TV 主观测试</Typography.Title>
        <Routes>
          <Route path="/" element={<CasesPage />} />
          <Route path="/cases/:caseId/plan" element={<PlanPage />} />
          <Route path="/runs" element={<CasesPage runsOnly />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/runs/:runId/report" element={<ReportPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout.Content>
    </Layout>
  );
}
