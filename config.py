"""全局配置：读取 .env，提供按角色回退的 LLM 配置。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
CHECKPOINT_DB = DATA_DIR / "checkpoints.sqlite"
APP_DB = DATA_DIR / "app.sqlite"

for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "5100"))
APP_DEBUG = os.getenv("APP_DEBUG", "1") == "1"

# 解析阶段 LLM 抽取并发批次数（大文档可调高以提速）
MAP_CONCURRENCY = int(os.getenv("MAP_CONCURRENCY", "8"))


def _role_env(role: str) -> dict:
    """读取某角色的 LLM 环境变量（未配置返回空）。"""
    prefix = f"LLM_{role.upper()}_"
    return {
        "base_url": os.getenv(prefix + "BASE_URL"),
        "api_key": os.getenv(prefix + "API_KEY"),
        "model": os.getenv(prefix + "MODEL"),
        "temperature": os.getenv(prefix + "TEMPERATURE"),
    }


def get_llm_config(role: str = "default") -> dict:
    """按角色返回 LLM 配置，缺省回退全局默认。"""
    cfg = {
        "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        "api_key": os.getenv("LLM_API_KEY", ""),
        "model": os.getenv("LLM_MODEL", "deepseek-chat"),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.2")),
        "timeout": float(os.getenv("LLM_TIMEOUT", "300")),
    }
    role_cfg = _role_env(role) if role != "default" else {}
    for k, v in (role_cfg or {}).items():
        if v not in (None, ""):
            cfg[k] = float(v) if k == "temperature" else v
    return cfg


def llm_mock_enabled() -> bool:
    return os.getenv("LLM_MOCK", "0") == "1"


def llm_cache_flavor(role: str = "extraction") -> str:
    """落盘缓存的来源标识：mock 与各真实模型的抽取结果不能互相复用。

    缓存 key 若只按输入文本哈希，先在 mock 模式跑过一遍的文档，
    之后切回真实模型会直接命中 mock 那份假数据，产物看着完整其实全是编的。"""
    if llm_mock_enabled():
        return "mock"
    return get_llm_config(role).get("model") or "default"
