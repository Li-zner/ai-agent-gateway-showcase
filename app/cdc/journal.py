"""CDC 变更日志落盘：JSONL 追加写 + 按日/按大小滚动 + 断点续传 checkpoint

落盘目录默认 cdc_journal/（docker-compose 挂载到宿主机 SSD），
写入使用 asyncio.to_thread 避免阻塞事件循环；每个 append_batch 批次
flush + fsync 一次，作为 checkpoint/cleanup 的持久化栅栏（INFRA-5 修注释漂移：
原先"每 100 条 fsync"描述的是已删除的 _write_line 死路径）。
"""
import asyncio
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from ..core.logging import setup_logging

logger = setup_logging()

_JOURNAL_NAME_RE = re.compile(r"^(\d{8})\.jsonl(?:\.\d+)?$")


def parse_jsonb(value):
    """把 asyncpg 返回的 JSONB 值规范化为结构化对象。

    asyncpg 默认把 jsonb 列解码成 JSON 文本 str；若直接塞进事件，json.dumps 会双重编码
    （before/after 变成 \"{...}\" 字符串，而非嵌套对象）。此处 str -> dict/list 解析一次，
    已是对象则原样返回；解析失败（如纯文本）则回退为原值（P1 修复双重编码）。
    """
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def format_ts(dt):
    """把数据库时间规范化为 UTC ISO 字符串（末尾带 Z）。

    TIMESTAMPTZ 列返回 aware datetime（可能非 UTC 会话时区），naive 视为 UTC。
    统一成 \"YYYY-MM-DDTHH:MM:SS(.ffffff)Z\"，保证 journal 与 API 的 ts 一致（P1 #11）。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CdcJournal:
    """按天追加 JSONL 的变更日志仓库"""

    def __init__(self, journal_dir: str):
        self.journal_dir = journal_dir
        self.checkpoint_path = os.path.join(journal_dir, "checkpoint.json")
        self._fh = None
        self._current_basename = None
        self._max_size = 50 * 1024 * 1024  # 单文件 50MB 后滚动
        self._write_lock = threading.Lock()  # P1 #7：防 to_thread 并发写交错
        self.last_id, self.last_txid = self.load_checkpoint_pair()  # 启动时恢复断点

    def _ensure_dir(self):
        os.makedirs(self.journal_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.journal_dir, 0o700)
        except OSError:  # noqa: silent-except 豁免：Windows/受限文件系统不支持 chmod
            pass

    @staticmethod
    def _open_append(path: str):
        """以 0600 权限打开追加文件，防支付镜像被同机其他用户读取。"""
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def _day_basename(self):
        return datetime.now().strftime("%Y%m%d") + ".jsonl"

    def _day_path(self):
        return os.path.join(self.journal_dir, self._day_basename())

    async def append_batch(self, events: list[tuple[dict, int, int]]) -> int:
        """整批写入并 fsync 后才推进水位，返回持久化到的最大 event id。"""
        if not events:
            return self.last_id
        lines = [
            json.dumps(event, ensure_ascii=False, default=str) + "\n"
            for event, _event_id, _txid in events
        ]
        await asyncio.to_thread(self._write_lines_durable, lines)
        self.last_id = events[-1][1]
        self.last_txid = events[-1][2]
        return self.last_id

    def _write_lines_durable(self, lines: list[str]):
        """批量写入后统一 flush + fsync，作为 checkpoint/cleanup 的持久化栅栏。"""
        with self._write_lock:
            self._ensure_dir()
            basename = self._day_basename()
            path = os.path.join(self.journal_dir, basename)
            if self._fh is None or self._current_basename != basename:
                new_fh = self._open_append(path)
                old_fh = self._fh
                self._fh = new_fh
                if old_fh:
                    old_fh.close()
                self._current_basename = basename
            for line in lines:
                self._rotate_if_needed(basename)
                self._fh.write(line)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def _rotate_if_needed(self, basename: str):
        """单文件超限时切到下一个滚动文件。

        此处刻意不更新 _current_basename（仍为"当日文件名"）——它只用于
        "跨天/首开"判断；若改成 f"{basename}.{seq}"，下一笔校验
        self._current_basename != basename 恒真，会反复重开超限的当日文件，
        导致每条写入都新建一个滚动文件（文件无限增殖，P2 #14 回归过）。
        保留当日名，写入自然落到滚动序号文件；序号上限 1000 防无限增长。
        """
        if self._fh.tell() <= self._max_size:
            return
        seq = 1
        while seq < 1000:
            alt = os.path.join(self.journal_dir, f"{basename}.{seq}")
            if not os.path.exists(alt):
                break
            seq += 1
        new_fh = self._open_append(alt)
        old_fh = self._fh
        self._fh = new_fh
        old_fh.close()

    async def save_checkpoint(self, last_id: int, last_txid: int = 0):
        """原子写 checkpoint（临时文件 + os.replace；记录当前 journal 文件名，P2 #18）

        单调保护：多实例部署时，非 leader 实例的 last_id 停留在启动时的旧值，
        停机时若直接覆写会把已推进的 checkpoint 回退，导致重启后重放已处理事件（重复）。
        这里先读盘，仅当 last_id 不低于当前值才写入（P1）。
        """
        current_id, current_txid = self.load_checkpoint_pair()
        if (last_txid, last_id) < (current_txid, current_id):
            logger.warning(f"checkpoint 回退被忽略: {last_id} < 当前值")
            return
        self.last_id = last_id
        self.last_txid = last_txid
        data = {
            "last_id": last_id,
            "last_txid": last_txid,
            "journal": self._current_basename or self._day_basename(),
            "updated_at": datetime.now().isoformat(),
        }
        await asyncio.to_thread(self._write_checkpoint, data)

    def prune_old_files(self, retention_days: int = 30) -> int:
        """删除超过保留期且非当前文件的 journal 文件，返回删除数量。"""
        if retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0
        for name in os.listdir(self.journal_dir):
            match = _JOURNAL_NAME_RE.match(name)
            if not match or name == self._current_basename:
                continue
            try:
                day = datetime.strptime(match.group(1), "%Y%m%d")
                path = os.path.join(self.journal_dir, name)
                if day < cutoff and os.path.isfile(path):
                    os.remove(path)
                    removed += 1
            except OSError as e:
                logger.warning(f"journal 清理失败: file={name}, err={e}")
        return removed

    def _write_checkpoint(self, data: dict):
        self._ensure_dir()
        # 唯一临时文件（2026-09-12 修复：多实例共用固定 .tmp 会互相覆盖半截内容）
        tmp = f"{self.checkpoint_path}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.checkpoint_path)

    def load_checkpoint_pair(self) -> "tuple[int, int]":
        """单次读取 checkpoint，原子返回 (last_id, last_txid) 水位对。

        2026-09-12 修复（外部复核 P0）：原 last_id 与 last_txid 分两次读文件，
        并发 os.replace 替换 checkpoint 时可能交叉得到"新 txid + 旧 id"的混合
        水位，消费游标跳过事件。所有读取方（初始化/刷新/保存比较）统一复用本函数。
        """
        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return int(data.get("last_id", 0)), int(data.get("last_txid", 0))
            except Exception as e:
                logger.warning(f"checkpoint 读取失败（{e}），按 0 处理")
        return 0, 0

    def load_checkpoint(self) -> int:
        return self.load_checkpoint_pair()[0]

    def refresh_checkpoint(self) -> int:
        """从磁盘重读 checkpoint 并刷新 self.last_id/last_txid。

        状态/查看接口（routes.py）每次调用刷新，避免实例在首次创建后 last_id 永久冻结
        （worker 另建实例写 checkpoint，本实例若不重读则永远显示旧值）。
        """
        self.last_id, self.last_txid = self.load_checkpoint_pair()
        return self.last_id

    def list_files(self):
        """列出落盘的日志文件信息（供 API 查看）"""
        self._ensure_dir()
        out = []
        for name in sorted(os.listdir(self.journal_dir)):
            if name.startswith("checkpoint"):
                continue
            p = os.path.join(self.journal_dir, name)
            try:
                if os.path.isfile(p):
                    out.append({"name": name, "size": os.path.getsize(p)})
            except OSError:
                # isfile 与 getsize 之间被 prune_old_files 删除（TOCTOU）：跳过即可，
                # 状态接口不因清理竞态 500（2026-10-06 审阅债转修）
                continue
        return out

    def close(self):
        # P1：与 _write_lines_durable 共用 _write_lock，避免停机时正在线程池中写文件、
        # 而 close() 并发关闭句柄造成的写入报错/文件损坏。
        with self._write_lock:
            if self._fh:
                try:
                    os.fsync(self._fh.fileno())
                except Exception as e:
                    logger.debug(f"journal fsync 失败（尽力而为）: {e}")
                self._fh.close()
                self._fh = None
