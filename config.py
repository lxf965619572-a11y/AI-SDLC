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


def _asset_version() -> str:
    """前端资源版本号：取 app.css / app.js 的最新修改时间。

    Flask 对 static 默认发 12 小时缓存，改完前端不强制刷新就还是旧界面；
    带上这个版本号，重启服务后浏览器自然拿新文件，第三方库仍走缓存。"""
    newest = 0.0
    for name in ("css/app.css", "js/app.js"):
        try:
            newest = max(newest, (BASE_DIR / "static" / name).stat().st_mtime)
        except OSError:
            pass
    return str(int(newest))


ASSET_VERSION = os.getenv("ASSET_VERSION", "") or _asset_version()

# 解析阶段 LLM 抽取并发批次数（大文档可调高以提速）
MAP_CONCURRENCY = int(os.getenv("MAP_CONCURRENCY", "8"))


# ===== 代码验证执行环境（远端 Linux）=====
# 为什么编译/运行/覆盖率都不在本机跑：目标平台是 Linux 工具链（gcc + gcov），
# Windows 上没有可信的 gcov 版本，且航天嵌入式后续要换成 QEMU 目标机模拟。
# 把「怎么执行」收在 runner 抽象后面，换执行环境只改一处。
VERIFY_HOST = os.getenv("VERIFY_HOST", "").strip()
VERIFY_USER = os.getenv("VERIFY_USER", "").strip()
VERIFY_PORT = int(os.getenv("VERIFY_PORT", "22"))
# 私钥路径；留空则用 ~/.ssh/id_ed25519_workbuddy_verify（由 scripts/setup_verify_vm.ps1 生成）
VERIFY_KEY = os.getenv("VERIFY_KEY", "").strip()
VERIFY_KNOWN_HOSTS = os.getenv("VERIFY_KNOWN_HOSTS", "").strip()
# 远端工作区根目录（相对 $HOME），实际路径为 ~/<VERIFY_WORKDIR>/p<项目>/v<版本>/
VERIFY_WORKDIR = os.getenv("VERIFY_WORKDIR", "wb_verify").strip() or "wb_verify"
VERIFY_TIMEOUT = int(os.getenv("VERIFY_TIMEOUT", "300"))
# =1 时忽略 VERIFY_HOST，改用本机工具链（Linux/macOS 开发机自测用）
VERIFY_LOCAL = os.getenv("VERIFY_LOCAL", "0") == "1"

# ===== 目标机矩阵（多指令集 / 字长 / 字节序）=====
# 宿主机跑绿只证明「在这台机器的 gcc 与 ABI 下行为正确」。星载软件的真实风险恰恰
# 落在宿主机测不出来的地方：long 是 4 还是 8 字节、大端还是小端、32 位截断、
# 结构体对齐。所以同一份源码要在多个目标架构上分别交叉编译 + qemu 执行 + 采覆盖率。
# 逗号分隔的目标 id，可选值见 verification/targets.py 的 TARGETS
# （host / arm32 / arm64 / ppc32）。默认只在验证机本机跑，证据布局与耗时不变。
#   VERIFY_TARGETS=host,arm32,ppc32
# 验证机一次性装交叉工具链：scripts/setup_verify_targets.sh（幂等，可重复执行）。
# 配了交叉目标而验证机没装工具链时，该目标记「未验证」并整轮转人工，绝不静默放行。
VERIFY_TARGETS = os.getenv("VERIFY_TARGETS", "").strip() or "host"

# ===== 验证判据 =====
# 分支覆盖率门限：低于该值的行在追溯矩阵里标红。0 表示不判覆盖率。
COVERAGE_BRANCH_MIN = float(os.getenv("COVERAGE_BRANCH_MIN", "80"))
COVERAGE_LINE_MIN = float(os.getenv("COVERAGE_LINE_MIN", "0"))
# 圈复杂度上限（静态规则 WB-C-006）
COMPLEXITY_MAX = int(os.getenv("COMPLEXITY_MAX", "10"))
# 执行失败 / 必查项违规的最大自动修复轮数，超出即出问题报告单转人工裁决
MAX_FIX_ROUNDS = int(os.getenv("MAX_FIX_ROUNDS", "2"))

# 验证证据（原始日志、gcov 文本、输入哈希）归档目录
VERIFY_EVIDENCE_DIR = DATA_DIR / "verify"
VERIFY_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def verify_key_path() -> str:
    """验证机私钥路径（未配置时取 setup 脚本的默认位置）。"""
    if VERIFY_KEY:
        return VERIFY_KEY.replace("\\", "/")
    return str(Path.home() / ".ssh" / "id_ed25519_workbuddy_verify").replace("\\", "/")


def verify_configured() -> bool:
    """是否配置了远端验证机。未配置时流水线退化为「只静态检查、不实际执行」。"""
    return bool(VERIFY_LOCAL or VERIFY_HOST)


def verify_target_ids() -> list:
    """解析 VERIFY_TARGETS → 目标 id 列表（顺序即执行顺序）。

    写错就抛 TargetError，不静默降级成只测宿主机：那样报告上仍写着
    「已在 N 个目标上验证」，而实际证据只有一个目标。"""
    from verification import targets as tgt_tab

    return tgt_tab.ids(VERIFY_TARGETS)


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
