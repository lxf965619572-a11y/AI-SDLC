"""验证执行环境抽象：SshRunner（远端 Linux）与 LocalRunner（本机工具链）。

第一性原理：验证结论要能当证据用，就必须满足两条——
  1. 执行环境是确定的、可复现的（同一份输入 + 同一套工具链 → 同一结论）；
  2. 每次执行的原始输出留痕（命令行、退出码、stdout/stderr、输入哈希）。
所以 runner 只做两件极窄的事：把文件送过去（sync）、把命令跑出来（run），
并且把 RunResult 原样带回，不在这一层做任何「看起来成功」的美化。

实现选择：直接 subprocess 调系统 OpenSSH（Windows 10+ 自带 ssh.exe / scp.exe / tar.exe），
不引 paramiko。理由：零新增依赖、密钥与 known_hosts 复用系统既有安全策略、
出问题时用户可以在终端里手敲同样的命令复现。
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


class RunnerError(Exception):
    """执行环境不可用或命令无法送达（不是「命令返回非 0」，那是正常结论）。"""


@dataclass
class RunResult:
    """一次远端命令执行的完整留痕。"""
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    cmd: str = ""
    cwd: str = ""
    transport: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """stdout + stderr 合并（构建日志按时间序混排，合并后更接近人眼看到的）"""
        parts = [p for p in (self.stdout, self.stderr) if p and p.strip()]
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "duration_s": round(self.duration_s, 3),
                "cmd": self.cmd, "cwd": self.cwd, "transport": self.transport,
                "stdout_chars": len(self.stdout or ""),
                "stderr_chars": len(self.stderr or "")}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _q(path: str) -> str:
    """POSIX shell 单引号转义（远端命令一律用它包路径，防空格与特殊字符）"""
    return shlex.quote(str(path))


def _as_bytes(value) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _decode(value) -> str:
    """subprocess 超时等场景下 stdout 可能是 bytes，统一成 str。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class BaseRunner:
    """runner 接口。子类只需实现 probe / sync / run 三个方法。"""

    kind = "base"

    def probe(self) -> dict:
        """探测工具链：{"ok", "kind", "target", "uname", "cores", "gcc", "gcov", ...}"""
        raise NotImplementedError

    def sync(self, files: dict, remote_dir: str) -> dict:
        """把 {相对路径: 文本/字节} 送到 remote_dir，返回 {相对路径: sha256}。

        哈希在送出之前于本机算好——证据链要证明「跑的就是这份代码」，
        哈希必须由编排侧计算并归档，不能事后从远端反推。"""
        raise NotImplementedError

    def run(self, cmd: str, cwd: str | None = None, timeout: int | None = None) -> RunResult:
        raise NotImplementedError

    # ---- 通用工具 ----
    root = "wb_verify"

    @property
    def root_abs(self) -> str:
        """工作区根的绝对路径。用绝对路径而不是 ~/…：远端命令一律经 shlex.quote
        包裹以防空格与特殊字符，而引号会让 ~ 失去展开语义。"""
        return self.root

    def workdir(self, project_id: int, version: int | str) -> str:
        """本项目本轮验证的远端工作目录：<root>/p<项目>/v<版本>。"""
        return f"{self.root_abs}/p{project_id}/v{version}"

    @staticmethod
    def manifest(files: dict) -> dict:
        """输入清单哈希：{相对路径: {sha256, bytes}}"""
        out = {}
        for rel, content in sorted(files.items()):
            data = _as_bytes(content)
            out[str(rel).replace("\\", "/")] = {
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        return out


PROBE_CMD = (
    'echo "uname=$(uname -srm)"; echo "cores=$(nproc 2>/dev/null || echo 1)"; '
    'echo "gcc=$(gcc --version 2>/dev/null | head -1)"; '
    'echo "gcov=$(gcov --version 2>/dev/null | head -1)"; '
    'echo "make=$(make --version 2>/dev/null | head -1)"; '
    'echo "tar=$(tar --version 2>/dev/null | head -1)"; '
    'echo "home=$HOME"; echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"'
)


def parse_probe(stdout: str) -> dict:
    """把 PROBE_CMD 的 key=value 输出解析成字典。"""
    out: dict = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


class SshRunner(BaseRunner):
    """经系统 OpenSSH 在远端 Linux 上执行。"""

    kind = "ssh"

    def __init__(self, host: str, user: str, port: int = 22, key: str = "",
                 known_hosts: str = "", root: str = "wb_verify",
                 timeout: int = 300):
        if not host or not user:
            raise RunnerError("SshRunner 需要 host 与 user")
        self.host, self.user, self.port = host, user, int(port or 22)
        self.key = (key or "").replace("\\", "/")
        self.known_hosts = (known_hosts or "").replace("\\", "/")
        self.root = root.strip("/") or "wb_verify"
        self.timeout = int(timeout or 300)
        self._probe: dict | None = None
        self._home: str | None = None

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"

    @property
    def root_abs(self) -> str:
        """远端工作区根绝对路径。root 配的是相对家目录的名字（如 wb_verify），
        用 probe 拿到的 $HOME 拼成绝对路径，避免 shlex.quote 让 ~ 失效。"""
        if self._home is None:
            info = self.probe()
            self._home = (info.get("home") or "").strip() or f"/home/{self.user}"
        return f"{self._home.rstrip('/')}/{self.root}"

    def _base_args(self) -> list[str]:
        args = ["ssh", "-p", str(self.port),
                "-o", "BatchMode=yes",                 # 绝不在后台线程里弹密码交互
                "-o", "StrictHostKeyChecking=yes",      # 主机公钥必须已钉住，变了就失败
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=4"]
        if self.key:
            if not Path(self.key).exists():
                raise RunnerError(f"验证机私钥不存在：{self.key}（先跑 scripts/setup_verify_vm.ps1）")
            args += ["-i", self.key]
        if self.known_hosts:
            args += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        args.append(f"{self.user}@{self.host}")
        return args

    def _scp_args(self) -> list[str]:
        args = ["scp", "-q", "-P", str(self.port), "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10"]
        if self.key:
            args += ["-i", self.key]
        if self.known_hosts:
            args += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        return args

    def run(self, cmd: str, cwd: str | None = None, timeout: int | None = None) -> RunResult:
        remote = f"cd {_q(cwd)} && {cmd}" if cwd else cmd
        argv = self._base_args() + [remote]
        t0 = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout or self.timeout)
            code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError as e:
            raise RunnerError(f"找不到 ssh 可执行文件：{e}") from e
        except subprocess.TimeoutExpired as e:
            return RunResult(exit_code=124,
                             stdout=_decode(e.stdout),
                             stderr=f"执行超时（>{timeout or self.timeout}s），已终止",
                             duration_s=time.time() - t0, cmd=remote,
                             cwd=cwd or "", transport=self.kind)
        return RunResult(exit_code=code, stdout=out, stderr=err,
                         duration_s=time.time() - t0, cmd=remote,
                         cwd=cwd or "", transport=self.kind)

    def probe(self, force: bool = False) -> dict:
        if self._probe and not force:
            return self._probe
        res = self.run(PROBE_CMD, timeout=30)
        info = parse_probe(res.stdout)
        self._probe = {
            "ok": res.ok and bool(info.get("gcc")),
            "kind": self.kind, "target": self.target,
            "exit_code": res.exit_code,
            "error": "" if res.ok else (res.output or "ssh 连接失败")[:600],
            **info,
            "root": self.root,
        }
        return self._probe

    def sync(self, files: dict, remote_dir: str) -> dict:
        manifest = self.manifest(files)
        if not files:
            self.run(f"mkdir -p {_q(remote_dir)}", timeout=30)
            return manifest
        with tempfile.TemporaryDirectory(prefix="wb_sync_") as tmp:
            for rel, content in files.items():
                dest = Path(tmp) / str(rel).replace("\\", "/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(_as_bytes(content))
            self.run(f"mkdir -p {_q(remote_dir)}", timeout=30)
            try:
                self._tar_pipe(tmp, remote_dir)
            except RunnerError:
                # 少数 Windows 环境 tar.exe 缺失或管道被拦：退回 scp 打包上传
                self._scp_bundle(tmp, remote_dir)
        return manifest

    def _tar_pipe(self, tmp: str, remote_dir: str) -> None:
        """tar 管道直传：本机打包 → ssh stdin → 远端解包。一次连接，无需中间文件。"""
        if not shutil.which("tar"):
            raise RunnerError("本机没有 tar，改用 scp 通道")
        remote = f"tar -xzf - -C {_q(remote_dir)}"
        ssh = subprocess.Popen(self._base_args() + [remote],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        tar = subprocess.Popen(["tar", "-czf", "-", "-C", tmp, "."],
                               stdout=ssh.stdin, stderr=subprocess.PIPE)
        try:
            ssh.stdin.close()
        except Exception:
            pass
        _, terr = tar.communicate(timeout=self.timeout)
        out, err = ssh.communicate(timeout=self.timeout)
        if tar.returncode != 0:
            raise RunnerError(f"本机打包失败：{(terr or b'').decode('utf-8', 'replace')}")
        if ssh.returncode != 0:
            raise RunnerError(
                f"远端解包失败（exit {ssh.returncode}）："
                f"{(err or b'').decode('utf-8', 'replace')[:400]}")

    def _scp_bundle(self, tmp: str, remote_dir: str) -> None:
        bundle = Path(tmp).parent / "wb_bundle.tgz"
        subprocess.run(["tar", "-czf", str(bundle), "-C", tmp, "."],
                       capture_output=True, check=True, timeout=self.timeout)
        dest = f"{self.user}@{self.host}:{remote_dir}/_wb_bundle.tgz"
        up = subprocess.run(self._scp_args() + [str(bundle), dest],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=self.timeout)
        if up.returncode != 0:
            raise RunnerError(f"scp 上传失败：{(up.stderr or up.stdout or '')[:400]}")
        res = self.run("tar -xzf _wb_bundle.tgz && rm -f _wb_bundle.tgz",
                       cwd=remote_dir, timeout=self.timeout)
        if not res.ok:
            raise RunnerError(f"远端解包失败：{res.output[:400]}")


class LocalRunner(BaseRunner):
    """本机执行（开发自测用）。要求本机有 POSIX 工具链：sh / gcc / gcov / tar。"""

    kind = "local"

    def __init__(self, root: str = "", timeout: int = 300):
        self.root = root or str(Path(tempfile.gettempdir()) / "wb_verify")
        self.timeout = int(timeout or 300)
        Path(self.root).mkdir(parents=True, exist_ok=True)

    @property
    def target(self) -> str:
        return f"local:{self.root}"

    @property
    def root_abs(self) -> str:
        return str(Path(self.root).resolve()).replace("\\", "/")

    def run(self, cmd: str, cwd: str | None = None, timeout: int | None = None) -> RunResult:
        shell = shutil.which("sh") or shutil.which("bash")
        argv = [shell, "-c", cmd] if shell else [os.environ.get("COMSPEC", "cmd"), "/c", cmd]
        if cwd:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout or self.timeout)
            code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError as e:
            raise RunnerError(f"本机缺少可用 shell：{e}") from e
        except subprocess.TimeoutExpired:
            return RunResult(exit_code=124, stderr=f"执行超时（>{timeout or self.timeout}s）",
                             duration_s=time.time() - t0, cmd=cmd, cwd=cwd or "",
                             transport=self.kind)
        return RunResult(exit_code=code, stdout=out, stderr=err,
                         duration_s=time.time() - t0, cmd=cmd, cwd=cwd or "",
                         transport=self.kind)

    def probe(self, force: bool = False) -> dict:
        res = self.run(PROBE_CMD.replace("nproc", "nproc 2>/dev/null || echo 1"), timeout=30)
        info = parse_probe(res.stdout)
        return {"ok": res.ok and bool(info.get("gcc")), "kind": self.kind,
                "target": self.target, "exit_code": res.exit_code,
                "error": "" if res.ok else (res.output or "本机探测失败")[:600],
                **info, "root": self.root}

    def sync(self, files: dict, remote_dir: str) -> dict:
        manifest = self.manifest(files)
        base = Path(remote_dir)
        if not base.is_absolute():
            base = Path(self.root) / remote_dir
        base.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            dest = base / str(rel).replace("\\", "/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_as_bytes(content))
        return manifest


class FakeRunner(BaseRunner):
    """单测替身：记录调用，按预置脚本返回结果，不碰真实进程与网络。"""

    kind = "fake"

    def __init__(self, responses: dict | None = None, probe: dict | None = None,
                 root: str = "wb_verify"):
        self.responses = dict(responses or {})
        self.calls: list[dict] = []
        self.files: dict[str, dict] = {}
        self.root = root
        self._probe = {"ok": True, "kind": self.kind, "target": "fake",
                       "uname": "Linux fake x86_64", "cores": "4",
                       "gcc": "gcc (fake) 7.5.0", "gcov": "gcov (fake) 7.5.0",
                       **(probe or {})}

    def probe(self, force: bool = False) -> dict:
        return dict(self._probe)

    @property
    def root_abs(self) -> str:
        return f"/fake/{self.root.strip('/')}"

    def run(self, cmd: str, cwd: str | None = None, timeout: int | None = None) -> RunResult:
        self.calls.append({"cmd": cmd, "cwd": cwd})
        for key, value in self.responses.items():
            if key in cmd:
                if isinstance(value, RunResult):
                    return value
                if isinstance(value, dict):
                    return RunResult(cmd=cmd, cwd=cwd or "", transport=self.kind, **value)
                return RunResult(exit_code=0, stdout=str(value), cmd=cmd,
                                 cwd=cwd or "", transport=self.kind)
        return RunResult(exit_code=0, stdout="", cmd=cmd, cwd=cwd or "",
                         transport=self.kind)

    def sync(self, files: dict, remote_dir: str) -> dict:
        self.calls.append({"cmd": f"<sync {len(files)} files>", "cwd": remote_dir})
        self.files[str(remote_dir)] = {str(k): _as_bytes(v) for k, v in files.items()}
        return self.manifest(files)


def make_runner(host: str = "", user: str = "", port: int = 22, key: str = "",
                known_hosts: str = "", root: str = "wb_verify",
                timeout: int = 300, local: bool = False) -> BaseRunner | None:
    """按参数造 runner；信息不足时返回 None（调用方据此降级为「不实际执行」）。"""
    if local:
        return LocalRunner(root=root, timeout=timeout)
    if not host or not user:
        return None
    return SshRunner(host=host, user=user, port=port, key=key,
                     known_hosts=known_hosts, root=root, timeout=timeout)


def runner_from_config(force: bool = False) -> BaseRunner | None:
    """从 config（即 .env）构造验证机 runner。未配置验证机时返回 None。"""
    import config

    if not config.verify_configured():
        return None
    return make_runner(host=config.VERIFY_HOST, user=config.VERIFY_USER,
                       port=config.VERIFY_PORT, key=config.verify_key_path(),
                       known_hosts=config.VERIFY_KNOWN_HOSTS,
                       root=config.VERIFY_WORKDIR, timeout=config.VERIFY_TIMEOUT,
                       local=config.VERIFY_LOCAL)
