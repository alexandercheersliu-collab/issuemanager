# 智能错题本 · 移动端模块

React + TypeScript + Vite + Tailwind + shadcn/ui 的移动端 Web（PWA 外壳），
直连 FastAPI REST 网关（默认 `http://localhost:8000/api`）。

## 启动

```bash
# 1. 先启动 API 网关（仓库根目录）
uvicorn api.main:app --port 8000

# 2. 启动移动端（本目录）
npm install
npm run dev
```

修改 API 地址：复制 `.env.example` 为 `.env` 并改 `VITE_API_BASE`；
网关侧通过 `API_CORS_ORIGINS` 放行本模块来源（默认 `*` 仅限内网开发）。

## 生产部署（家庭局域网）

`npm run build` 后，FastAPI 网关会把 `mobile/dist` 自动挂载到 `/m`
（构建时 `.env.production` 已固定 `VITE_API_BASE=/api` 同源相对路径，免 CORS）：

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
# 手机/平板连同一 Wi-Fi，访问 http://<电脑IP>:8000/m/
```

完整步骤（防火墙、固定 IP、开机自启）见 `docs/DEPLOYMENT.md` 第 1.5 节。

## 功能

- **复习**：到期错题闪卡（题面/解析分离、原图折叠），四档 SM-2 评分，2×2 触控按钮
- **录题**：拍照/相册上传（`capture="environment"` 唤起相机），标签与备注可选
- **整卷导入**（录题页底部入口）：多页照片客户端合成 PDF（canvas 压至 1600px/JPEG 0.8，
  jspdf 按需动态加载）→ `POST /documents/import` → 轮询 `/documents/{job_id}/segments`
  看切题/解构进度 → 校对列表（可删除误切题）→ `POST /documents/{job_id}/confirm` 批量入库
- **错题本**：语义搜索（防抖）+ 分页加载 + 详情弹窗
- **我的**：看板统计（累计/待复习/已复习/已掌握/连续天数）+ 退出登录

会话：JWT 存 localStorage，启动时 `GET /auth/me` 恢复；任一请求 401 全局登出，
429 按 `Retry-After` 提示。错题原图经 `GET /questions/{id}/image` 鉴权拉取（blob → objectURL）。
