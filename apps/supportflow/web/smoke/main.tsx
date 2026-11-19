import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Typography } from "antd";
import { createRoot } from "react-dom/client";

const root = document.getElementById("root");
if (!root) throw new Error("缺少挂载节点 #root");

createRoot(root).render(
  <QueryClientProvider client={new QueryClient()}>
    <Typography.Text>SupportFlow 前端环境</Typography.Text>
  </QueryClientProvider>,
);
