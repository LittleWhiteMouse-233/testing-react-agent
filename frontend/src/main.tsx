import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider } from "antd";
import { BrowserRouter } from "react-router-dom";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } }
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: "#0066cc", colorText: "#1d1d1f", colorBorder: "#e0e0e0", borderRadius: 8, fontSize: 13, fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif' } }}>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </ConfigProvider>
  </React.StrictMode>
);
