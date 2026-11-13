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

- **复习**：到期错题闪卡（题面/解析分离、原图折叠），四档 SM-2 评分，2×2 触控按钮。
- **录题**：现有拍照/相册入口支持多选 JPEG/PNG/WEBP、预览、可选标签与备注。
  多题照片进入候选勾选，疑似错题默认选中；单题、空切题或切题失败自动按整图录入。
  结果页区分入库、重复和失败，刷新后可恢复未完成任务。
- **整卷导入**（录题页底部入口）：直接上传一份 PDF/DOCX，或按页序上传最多 20 张
  JPEG/PNG/WEBP，不再先在客户端合成 PDF。异步显示切题/解构进度；校对页可删除误切题、
  重新解析单题，确认后展示入库与跳过数量及可展开的跳过原因，并支持未确认任务恢复。
- **错题本与图形**：语义搜索、分页和详情弹窗沿用原入口；题面/解析使用服务端字段，
  题目、整卷校对和同类题检测中的有效图通过鉴权接口显示。无有效图时按既有规则回退到
  原图/原页；原图裁剪兜底只显示简短准确性提示，不展示 TikZ 源码。
- **我的**：看板统计（累计/待复习/已复习/已掌握/连续天数）+ 退出登录。

两个录题入口均使用默认上下文：数学、10 年级、上海、沪教版；Mobile 不提供 K12 表单、
文字录入、题干/解析编辑、合并/拆分或 TikZ 编辑器。注册时采集学生偏好并替代默认值是
后续工作，当前未实现。

会话：JWT 存 localStorage，启动时 `GET /auth/me` 恢复；任一请求 401 全局登出，
429 按 `Retry-After` 提示。错题原图和有效图经鉴权 API 拉取（blob → objectURL）。

## Mobile E2E

先构建本目录，再从仓库根目录启动 API 并检查健康状态：

```bash
npm ci
npm run build
# 仓库根目录
uvicorn api.main:app --port 8000 &
curl --retry 30 --retry-delay 1 --retry-connrefused -f http://localhost:8000/health
MM_MOBILE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_mobile_import.py -q
```

未设置 `MM_MOBILE_E2E_BASE_URL` 时，测试会自动启动带临时数据库、临时上传目录和受控
MockProvider 的隔离 API（随机空闲端口），并复用刚构建的 `mobile/dist`；不会向上述 8000
服务的开发数据库写入测试数据。设置该变量可改用已有的兼容 `/m/` 地址。失败截图写到
仓库根目录的 `e2e_failure_mobile_*.png`，CI 会上传这些文件。
