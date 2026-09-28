# E2E 冒烟测试

Playwright headless 冒烟：登录 / 看板 / 全页面导航 / 录题全流程 / 追问讲题。

## 运行

```bash
# 1. 启动应用（独立端口，别占用日常 8501）
python -m streamlit run app.py --server.port 8601 --server.headless true

# 2. 跑套件
set MM_E2E=1 && pytest tests/e2e -q -m e2e          # Windows
MM_E2E=1 pytest tests/e2e -q -m e2e                 # bash
MM_E2E_BASE_URL=http://localhost:8602 ...           # 非默认端口时覆盖
```

## 已知环境限制：headless 下侧边栏菜单 iframe 不挂载

在本机 headless Chromium 中，streamlit-antd-components 的菜单 iframe
（`about:srcdoc`）可能渲染为空，导致所有经过 `_goto()` 点击菜单的用例超时失败
（`waiting for locator("iframe[title*='streamlit_antd_components']")...`）。

这是**环境问题而非应用回归**：真实浏览器中菜单正常；用同一探针打日常实例
（改动前的代码）现象相同。判读失败时先看失败点是否都在 `_goto`：

- 失败全在 `_goto` → 环境限制，用真实浏览器手动过一遍导航即可；
- `test_login_and_dashboard` 也挂 → 才是真回归（它不经过菜单 iframe）。

CI（GitHub Actions 干净环境）不受此限制；本机如需完整跑通，可改用
有头模式（`conftest.py` 里 `launch(headless=False)`）或升级/降级
streamlit-antd-components 后再验证。
