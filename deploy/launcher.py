"""Issues Manager 一体化启动器（Windows 部署包 exe 入口，开发模式亦可直接运行）。

职责：
1. 注入运行时目录环境变量（数据落 exe 同级 data/，嵌入模型用包内预置）；
2. 首次运行引导配置大模型（交互向导：选服务商 → 填 Key → 真实调用验证 → 写 .env）；
3. 拉起双服务：uvicorn（8000，API + 移动端 /m）+ Streamlit（8501，桌面端）；
4. 打印本机/局域网访问地址并自动打开浏览器；Ctrl+C 优雅退出。

开发模式（``python -m deploy.launcher``）与 PyInstaller frozen 模式共用本文件：
目录推断按 frozen 状态切换。重型依赖（streamlit/uvicorn/项目代码）全部延迟到
main() 内 import，保证向导向导纯函数可脱离这些依赖单测。
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    EXE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:  # 开发模式：本文件在 <项目根>/deploy/ 下
    EXE_DIR = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = EXE_DIR

ENV_PATH = EXE_DIR / ".env"

# 大模型服务商预设：名称、provider 取值、BASE_URL、推荐视觉模型、Key 申请入口
PROVIDERS: list[dict] = [
    {
        "name": "SiliconFlow（硅基流动）",
        "provider": "openai_compatible",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-VL-32B-Instruct",
        "signup": "https://cloud.siliconflow.cn",
    },
    {
        "name": "通义千问（阿里云百炼）",
        "provider": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-plus",
        "signup": "https://bailian.console.aliyun.com",
    },
    {
        "name": "智谱 GLM",
        "provider": "openai_compatible",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-flash",
        "signup": "https://open.bigmodel.cn",
    },
    {
        "name": "DeepSeek",
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "signup": "https://platform.deepseek.com",
    },
    {
        "name": "Ollama（本机自建，无需 Key）",
        "provider": "openai_compatible",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5vl:7b",
        "signup": "https://ollama.com",
    },
    {
        "name": "Google Gemini",
        "provider": "gemini",
        "base_url": "",
        "model": "gemini-2.0-flash",
        "signup": "https://aistudio.google.com/apikey",
    },
]

ENV_TEMPLATE = """\
# ===== Issues Manager 环境配置（由配置向导生成）=====
AI_PROVIDER={provider}
AI_BASE_URL={base_url}
AI_API_KEY={api_key}
AI_MODEL={model}

# 令牌签名密钥（向导已自动生成强随机值，勿泄露）
AUTH_SECRET={auth_secret}

# 首次运行的种子账号（仅在数据库为空时生效，请登录后尽快改密）
SEED_ADMIN_USERNAME=admin
SEED_ADMIN_PASSWORD=admin123
SEED_DEMO_USERNAME=demo
SEED_DEMO_PASSWORD=demo123
"""


def inject_runtime_env() -> None:
    """把数据目录钉在 exe 同级；包内预置嵌入模型优先（离线可用）。"""
    os.environ.setdefault("DATA_DIR", str(EXE_DIR / "data"))
    os.environ.setdefault("CHROMA_DIR", str(EXE_DIR / "data" / "chroma"))
    bundled_model = BUNDLE_DIR / "models" / "onnx"
    if bundled_model.is_dir():
        os.environ.setdefault("CHROMA_MODEL_DIR", str(bundled_model))


def render_env(provider: str, base_url: str, api_key: str, model: str, auth_secret: str) -> str:
    """生成 .env 文本（纯函数，可单测）。"""
    return ENV_TEMPLATE.format(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        auth_secret=auth_secret,
    )


def needs_config(env_path: Path = ENV_PATH) -> bool:
    """判定是否要进入配置向导：.env 缺失，或仍是 mock/空 Key。"""
    if not env_path.exists():
        return True
    text = env_path.read_text(encoding="utf-8", errors="ignore")
    has_key = any(
        line.startswith("AI_API_KEY=") and line.split("=", 1)[1].strip()
        for line in text.splitlines()
    )
    return not has_key


def validate_api_key(provider: str, base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """发起一次真实小调用验证 Key 可用；返回 (是否通过, 提示信息)。"""
    try:
        if provider == "gemini":
            from google import genai

            client = genai.Client(api_key=api_key)
            client.models.generate_content(model=model, contents="ping")
            return True, "验证通过"
        from openai import OpenAI

        client = OpenAI(api_key=api_key or "ollama", base_url=base_url, timeout=30)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return True, "验证通过"
    except Exception as exc:  # noqa: BLE001 - 任何失败都回报给用户重试
        return False, f"验证失败：{exc}"


def _prompt(message: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{message}{suffix}: ").strip()
    return value or default


def run_wizard(env_path: Path = ENV_PATH) -> None:
    """首启配置向导：选服务商 → 填 Key → 验证 → 写 .env（含强随机 AUTH_SECRET）。"""
    print("\n===== 首次运行：配置大模型 =====")
    print("错题解析需要视觉大模型。请选择服务商：")
    for i, p in enumerate(PROVIDERS, 1):
        print(f"  {i}. {p['name']}（{p['signup']}）")
    print("  0. 暂不配置，以演示模式运行（之后可执行 IssuesManager.exe --reconfigure）")

    choice = _prompt("请输入编号", "0")
    if choice == "0" or not choice.isdigit() or not (1 <= int(choice) <= len(PROVIDERS)):
        if choice != "0":
            print("输入无效，按演示模式继续。")
        if not env_path.exists():
            env_path.write_text(
                render_env("mock", "", "", "", secrets.token_hex(32)), encoding="utf-8"
            )
            print(f"已写入 {env_path}（演示模式）。")
        return

    preset = PROVIDERS[int(choice) - 1]
    base_url = _prompt("BASE_URL", preset["base_url"])
    model = _prompt("模型（需具备视觉能力）", preset["model"])
    while True:
        api_key = _prompt("API Key（Ollama 可直接回车）")
        print("正在验证 Key…")
        ok, message = validate_api_key(preset["provider"], base_url, api_key, model)
        print(message)
        if ok:
            break
        if _prompt("重试？(y/n)", "y").lower() != "y":
            print("已跳过验证，将按所填内容写入（如不可用可 --reconfigure 重配）。")
            break

    env_path.write_text(
        render_env(preset["provider"], base_url, api_key, model, secrets.token_hex(32)),
        encoding="utf-8",
    )
    print(f"配置已写入 {env_path}\n")


def lan_ip() -> str:
    """取本机局域网 IP（UDP 连接 trick，不真正发包）；失败回退 127.0.0.1。"""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def start_api_server() -> object:
    """后台线程启动 uvicorn（8000：API + 移动端 /m），返回 server 便于退出。"""
    import uvicorn

    from api.main import create_app

    config = uvicorn.Config(create_app(), host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    return server


def main() -> None:
    # Windows 控制台默认 GBK；en-US 系统（cp1252）打印中文会直接 UnicodeEncodeError，
    # 降级为替换字符保证不崩（中文系统不受影响）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 非文本流（重定向/管道）时忽略
            pass

    parser = argparse.ArgumentParser(description="Issues Manager 一体化启动器")
    parser.add_argument("--reconfigure", action="store_true", help="重新运行大模型配置向导")
    args = parser.parse_args()

    inject_runtime_env()
    if args.reconfigure or needs_config():
        run_wizard()
    else:
        print("提示：当前为演示模式时可执行 IssuesManager.exe --reconfigure 配置大模型。")

    api_server = start_api_server()

    ip = lan_ip()
    print("\n===== Issues Manager 已启动 =====")
    print("  桌面端（本机）:  http://localhost:8501")
    print(f"  手机端（同 Wi-Fi）: http://{ip}:8000/m/")
    print("  首次手机访问若弹防火墙提示，请允许专用网络访问。按 Ctrl+C 停止。\n")
    threading.Timer(2.0, lambda: webbrowser.open("http://localhost:8501")).start()

    from streamlit import config as st_config
    from streamlit.web import bootstrap as st_bootstrap

    # bootstrap.run 不在启动时应用 flag_options（仅在配置文件变更时覆盖），
    # 必须先 set_option。缺了这一步，PyInstaller 环境下 developmentMode 会因
    # 「路径不含 site-packages」误判为 True：静态前端不挂载（/ 一律 404）、
    # server.port 被忽略。这里逐项显式设置，再原样传入 flag_options 供热重载。
    flags = {
        "server.port": 8501,
        "server.address": "0.0.0.0",
        "server.headless": True,
        "browser.gatherUsageStats": False,
        "global.developmentMode": False,
    }
    for name, value in flags.items():
        st_config.set_option(name, value)

    app_py = BUNDLE_DIR / "app.py"
    try:
        st_bootstrap.run(str(app_py), False, [], flags)
    except KeyboardInterrupt:
        pass
    finally:
        api_server.should_exit = True


if __name__ == "__main__":
    main()
