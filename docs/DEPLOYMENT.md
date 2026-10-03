# 部署指南 (Deployment Guide)

## 部署形态总览

| 形态 | 适用场景 | 说明 |
|---|---|---|
| 本地运行 | 演示 / 日常使用 | `streamlit run app.py`，SQLite + 内置嵌入模型，零外部依赖 |
| Windows 免安装包 | 客户交付（无开发环境） | PyInstaller onedir 单 exe，解压即用、首启配置向导，见「1.6 Windows 部署包」 |
| 家庭局域网 | 电脑做服务端，手机/平板同 Wi-Fi 使用 | 见「1.5 家庭局域网部署」，移动端由 API 网关同源托管 |
| Docker Compose | 云服务器 / 局域网 | Web + API 双服务，数据卷持久化 |
| Streamlit Cloud | 纯前端演示 | 仅 Web 层；演示模式（无 Key）或配 Secret |

## 1. 本地运行

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

streamlit run app.py          # Web: http://localhost:8501
uvicorn api.main:app --port 8000   # API: http://localhost:8000/docs（可选）
```

首次启动自动建表并创建种子账号（`SEED_*` 环境变量可改），请立即在「设置」中修改密码。

## 1.5 家庭局域网部署（电脑做服务端，手机/平板同 Wi-Fi 使用）

移动端是纯静态构建产物，**由 FastAPI 网关同源托管在 `/m`**：一个 8000 端口同时服务
API 与移动端页面，天然免 CORS，手机浏览器直接访问即可。

### 一次性准备（在电脑上）

```bash
# 1. 构建移动端（VITE_API_BASE=/api 已由 mobile/.env.production 固定，同源相对路径）
cd mobile
npm install
npm run build          # 产物在 mobile/dist，网关启动时自动挂载到 /m
cd ..
```

### 每次启动（在电脑上）

```bash
# API + 移动端（8000）：--host 0.0.0.0 允许局域网访问
uvicorn api.main:app --host 0.0.0.0 --port 8000

# 教师端 Web（8501，可选，仅电脑上用）
streamlit run app.py --server.address 0.0.0.0
```

### 手机 / 平板访问

1. 查电脑局域网 IP：Windows 执行 `ipconfig`，取「IPv4 地址」（如 `192.168.1.100`）；
2. 手机/平板连**同一 Wi-Fi**，浏览器打开 `http://192.168.1.100:8000/m/`；
3. 首次访问若被 Windows 防火墙拦截：允许 `python.exe` 通过专用网络的入站连接
   （弹出授权框直接勾选即可，或「高级安全防火墙」手动加 8000 入站规则）；
4. 浏览器菜单选「添加到主屏幕」，之后像 App 一样全屏打开
   （局域网 HTTP 下 PWA 离线缓存不可用，但图标与全屏模式可用）。

### 固定电脑 IP（建议）

路由器 DHCP 可能换 IP。二选一：

- 路由器后台给电脑 MAC 地址做「地址绑定/静态租约」（推荐，不改电脑设置）；
- 或 Windows 网络适配器手动设静态 IP。

### 开机自启（可选）

Windows「任务计划程序」建一个登录时触发的任务，操作填：

```
程序: <项目目录>\.venv\Scripts\python.exe
参数: -m uvicorn api.main:app --host 0.0.0.0 --port 8000
起始于: <项目目录>
```

### 安全说明

- 局域网 HTTP 明文传输，仅限家庭可信网络；**不要把 8000 端口映射到公网**（要公网用请走
  Docker Compose + Nginx/Caddy TLS，见第 2 节）；
- `AUTH_SECRET` 务必改成强随机值（令牌签名密钥），`API_CORS_ORIGINS` 同源部署下保持默认即可
  （同源不需要 CORS 放行）。


## 1.6 Windows 免安装部署包（客户交付）

面向无 Python 环境的 Windows 客户：**解压即用、无源码、首启引导配置大模型**。

### 包内容

```
IssuesManager-<version>-windows-x64.zip
├── IssuesManager.exe      # 一体化启动器（配置向导 + 双服务 + 自动开浏览器）
├── _internal/             # PyInstaller onedir 依赖（含预置嵌入模型，离线可用）
├── USER_MANUAL.md / .pdf  # 用户手册
└── data/                  # 首次启动生成（SQLite / 原图 / 向量库 / 日志）
```

启动器行为（`deploy/launcher.py`，开发模式也可 `python -m deploy.launcher` 调试）：

- 数据目录钉在 exe 同级 `data/`（`DATA_DIR`/`CHROMA_DIR`），嵌入模型用包内预置
  （`CHROMA_MODEL_DIR=_internal/models/onnx`）；
- `.env` 缺失或无 Key 时进入控制台配置向导：选服务商预设 → 填 Key → 真实调用验证 →
  写 `.env`（含自动生成的强随机 `AUTH_SECRET`）；`--reconfigure` 可随时重配；
- 同进程拉起 uvicorn（8000，API + 移动端 `/m`）与 Streamlit（8501），
  打印本机/局域网地址并打开浏览器；Ctrl+C 优雅退出。

### 构建（维护者）

GitHub Actions 手动触发 `build-windows-package`（`.github/workflows/build-windows.yml`）：
windows-latest → 预下载嵌入模型 → PyInstaller 按 `deploy/issues_manager.spec` 构建 →
reportlab 生成手册 PDF（msyh）→ 打 zip 传 artifact。
本地可先跑 Linux 烟测验证依赖收集（PyInstaller 不支持跨平台出 Windows exe）：

```bash
pip install pyinstaller
pyinstaller deploy/issues_manager.spec --noconfirm --distpath dist_smoke --workpath build_smoke
./dist_smoke/IssuesManager/IssuesManager   # 验证 8501/8000/m 均可达
```

## 2. Docker Compose（推荐）

```bash
cp .env.example .env    # 填入 AI_API_KEY 与强随机 AUTH_SECRET
docker compose up -d --build
```

- Web: `8501`；API: `8000`
- 数据（SQLite、图片、Chroma 向量库）持久化在 `mathmaster-data` 卷
- 健康检查：`curl http://localhost:8501/_stcore/health`、`curl http://localhost:8000/health`

生产建议：

1. `AUTH_SECRET` 用 `python -c "import secrets; print(secrets.token_hex(32))"` 生成；
2. 用 Nginx/Caddy 反向代理并配置 TLS，收紧 CORS（`api/main.py` 中 `allow_origins`）；
3. 挂载卷注意备份（见下文数据备份）。

## 3. 切换 MySQL（可选）

```env
DATABASE_URL=mysql+pymysql://mathmaster:mathmaster@localhost:3306/math_tutor?charset=utf8mb4
```

- 需要安装驱动：`pip install pymysql`
- 取消 `docker-compose.yml` 中 mysql 服务的注释即可联动
- 表结构由 SQLAlchemy `create_all` 自动创建；已有 SQLite 数据可用「设置 → 数据备份」导出 JSON 后在新库导入

### 数据库迁移（Alembic）

schema 变更通过 Alembic 管理（`migrations/`）：

```bash
# 全新环境：建表到最新版本
alembic upgrade head

# 已有的旧库（由 create_all 创建、无迁移记录）：补盖章后即可跟进后续迁移
alembic stamp head

# 修改 ORM 模型后生成迁移脚本
alembic revision --autogenerate -m "描述变更"

# 回退一个版本
alembic downgrade -1
```

数据库 URL 优先级：`alembic -x url=...` > 环境变量 `DATABASE_URL` > `backend/config.py`。

## 4. AI 提供商配置

任选一家 OpenAI 兼容服务（`.env`）：

```env
AI_PROVIDER=openai_compatible
AI_BASE_URL=https://api.siliconflow.cn/v1
AI_API_KEY=sk-xxxx
AI_MODEL=Qwen/Qwen2.5-VL-32B-Instruct
```

中文检索效果更佳可再配远程嵌入（可选）：

```env
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=sk-xxxx
EMBEDDING_MODEL=BAAI/bge-m3
```

不配置任何 Key 时应用以演示模式运行（MockProvider），便于验收部署是否成功。

## 5. 数据备份与迁移

- **界面**：设置 → 数据备份 → 导出备份 (JSON) / 导入备份
- **API**：`GET /api/questions/export`、`POST /api/questions/import`
- 题目原图存于 `data/images/`，向量库存于 `data/chroma/`；Docker 部署时两者均在数据卷内，直接备份卷即可

### 5.1 把运行时数据放到项目外（推荐）

默认所有运行时数据都在项目内 `data/`。希望数据库与代码、`.venv` 分离（例如放到另一块盘）时，
在 `.env` 里显式指定即可，三个目录各自独立、可单独迁移：

```ini
DATA_DIR=D:\workspace\learning-tour\data            # 根目录：SQLite DB / 上传原图 / 遥测日志
CHROMA_DIR=D:\workspace\learning-tour\data\chroma   # 向量库
CHROMA_MODEL_DIR=D:\workspace\learning-tour\data\models\onnx   # 内置嵌入模型缓存
# DATABASE_URL 留空 = sqlite:///{DATA_DIR}/math_tutor.db；也可显式写绝对路径
```

迁移步骤：

1. 停掉应用（避免 SQLite WAL 文件不一致）；
2. 拷贝整个旧 `data/` 到新 `DATA_DIR`，地址库本体是 `math_tutor.db` + `-wal`/`-shm` 三件套，一起拷；
3. 写好 `.env`，启动后在「设置 → 存储」确认路径已生效。

### 5.2 内置嵌入模型（all-MiniLM-L6-v2）安装

未配置远程 `EMBEDDING_*` 时使用 ChromaDB 内置 ONNX 模型。ChromaDB 官方实现把缓存路径写死为
`~/.cache/chroma/onnx_models`（Windows 即 `C:\Users\<用户>\.cache\chroma\...`），且下载**不支持断点续传**，
弱网下容易留下哈希不符的残包，导致每次启动重复下载。

本仓库提供带断点续传的安装脚本，可反复执行（断线后重跑会接着上次进度）：

```bash
python scripts/install_onnx_model.py            # 装到 CHROMA_MODEL_DIR（未配置则用 ChromaDB 默认位置）
python scripts/install_onnx_model.py --check-only
```

脚本会校验官方 SHA256、解压并做一次真实嵌入自检，成功后可离线启动 RAG。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 登录后白屏 | 检查浏览器控制台；确认 `.streamlit/config.toml` 存在且未损坏 |
| AI 解析 502 | 检查 `AI_API_KEY` 是否有效、模型是否有视觉能力（VL 系列而非纯文本模型） |
| `database is locked` | 已内置 WAL + 30s busy timeout；仍出现请确认没有多个进程共用同一 SQLite 文件且频繁写 |
| 向量库不可用 | 设置页会显示降级提示；检查 `data/chroma` 目录权限，或删除该目录重启（会重建索引，需重新录题） |
| 首次检索卡住/反复下载模型 | ChromaDB 官方下载无断点续传；改用 `python scripts/install_onnx_model.py` 安装到 `CHROMA_MODEL_DIR` |
| GitHub 连接失败 | 网络间歇受限；稍后重试或配置代理后 `git push` |
