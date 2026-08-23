"""并发安全的缓存管理器。

主要改进：
1. 原子写入（temp + rename）
2. 文件锁防止并发冲突
3. 自动重试机制
4. 损坏文件自动恢复
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import config


class CacheManager:
    """线程安全的缓存管理器"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        """根据key计算缓存文件路径"""
        hash_key = hashlib.md5(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{hash_key}.json"

    def _lock_path(self, cache_path: Path) -> Path:
        """缓存文件对应的锁文件"""
        return cache_path.with_suffix(".lock")

    def _acquire_lock(self, lock_path: Path, timeout: float = 5.0) -> Optional[int]:
        """获取文件锁（跨进程）"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, str(os.getpid()).encode())
                return fd
            except FileExistsError:
                time.sleep(0.05)
        return None

    def _release_lock(self, lock_path: Path, fd: int):
        """释放文件锁"""
        try:
            os.close(fd)
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    def get(self, key: str) -> Optional[dict]:
        """读取缓存（线程安全）"""
        cache_path = self._cache_path(key)

        if not cache_path.exists():
            return None

        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, IOError) as e:
            from core.errors import ErrorCategory, ErrorContext, record_error, ErrorSeverity
            record_error(
                category=ErrorCategory.RESOURCE,
                severity=ErrorSeverity.WARNING,
                message=f"缓存文件损坏: {cache_path.name}",
                context=ErrorContext(),
                exception=e
            )
            cache_path.unlink(missing_ok=True)
            return None

    def set(self, key: str, value: dict, max_retries: int = 3) -> bool:
        """写入缓存（原子操作，线程安全）"""
        cache_path = self._cache_path(key)
        lock_path = self._lock_path(cache_path)

        for attempt in range(max_retries):
            fd = self._acquire_lock(lock_path, timeout=2.0)
            if fd is None:
                continue

            try:
                with tempfile.NamedTemporaryFile(
                    mode='w',
                    encoding='utf-8',
                    dir=self.cache_dir,
                    delete=False,
                    suffix='.tmp'
                ) as tmp_file:
                    json.dump(value, tmp_file, ensure_ascii=False, indent=2)
                    tmp_path = Path(tmp_file.name)

                if os.name == 'nt' and cache_path.exists():
                    cache_path.unlink()
                tmp_path.replace(cache_path)

                return True

            except Exception as e:
                if 'tmp_path' in locals():
                    tmp_path.unlink(missing_ok=True)

                from core.errors import ErrorCategory, ErrorContext, record_error, ErrorSeverity
                record_error(
                    category=ErrorCategory.RESOURCE,
                    severity=ErrorSeverity.WARNING,
                    message=f"缓存写入失败: {cache_path.name}",
                    context=ErrorContext(),
                    exception=e
                )

            finally:
                self._release_lock(lock_path, fd)

        return False

    def delete(self, key: str):
        """删除缓存"""
        cache_path = self._cache_path(key)
        cache_path.unlink(missing_ok=True)

    def clear(self):
        """清空所有缓存"""
        for file in self.cache_dir.glob("*.json"):
            file.unlink(missing_ok=True)
        for file in self.cache_dir.glob("*.lock"):
            file.unlink(missing_ok=True)

    def stats(self) -> dict:
        """缓存统计"""
        files = list(self.cache_dir.glob("*.json"))
        total_size = sum(f.stat().st_size for f in files)
        return {
            "count": len(files),
            "total_size_mb": round(total_size / 1024 / 1024, 2),
        }


_parse_cache = CacheManager(config.DATA_DIR / "parse_cache")


def get_parse_cache() -> CacheManager:
    """获取解析缓存管理器"""
    return _parse_cache
