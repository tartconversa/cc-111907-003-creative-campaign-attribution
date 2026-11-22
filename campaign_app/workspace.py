"""隔离工作目录、原子状态与追加式审计链。

* 每个工作目录拥有独立的状态文件、导入记录和审计日志；互不相关的工作目录
  不会读写彼此的预算、方案或观察结果。
* 状态写入使用临时文件 + ``os.replace`` 原子替换，并在写入期间持有目录锁。
* 审计日志只追加；每条事件带前一条的哈希，可用 ``verify_audit`` 检查是否被
  静默改写或删除。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterator

from .source import DATASETS, SourceData, load_source
from .timeutil import format_ts, parse_ts

WORKSPACE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATE_VERSION = 1


class WorkspaceError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def hash_event(event: dict[str, Any], previous: str) -> str:
    body = {key: value for key, value in event.items() if key != "hash"}
    return hashlib.sha256((previous + "\n" + canonical_json(body)).encode("utf-8")).hexdigest()


def default_state(name: str, created_at: datetime, budget_total_fen: int,
                  inputs_dir: str = "") -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "workspace": {
            "name": name,
            "created_at": format_ts(created_at),
            "budget_total_fen": budget_total_fen,
            "currency": "CNY",
            "inputs_dir": inputs_dir,
        },
        "counters": {"design": 0, "review": 0, "plan": 0, "execution": 0, "decision": 0},
        "designs": {},
        "reviews": [],
        "plans": {},
        "executions": {},
        "contract_events": [],
        "last_recheck": None,
    }


@dataclass
class Workspace:
    root: Path
    state: dict[str, Any]
    inputs_dir: Path

    # ---- 创建与打开 ------------------------------------------------
    @classmethod
    def create(
        cls,
        work_root: Path,
        name: str,
        *,
        inputs_dir: Path,
        budget_total_fen: int = 600_000,
        now: datetime | None = None,
        replace: bool = False,
    ) -> "Workspace":
        if not WORKSPACE_NAME.match(name):
            raise WorkspaceError("工作目录名只能包含字母、数字、点、下划线和连字符，且不超过 64 字符")
        if budget_total_fen <= 0:
            raise WorkspaceError("预算总额必须为正整数分")
        inputs_dir = Path(inputs_dir).resolve()
        root = (Path(work_root) / name).resolve()
        if root == inputs_dir or inputs_dir in root.parents or root in inputs_dir.parents:
            raise WorkspaceError("工作目录不能与来源目录重叠")
        if root.exists():
            if not replace:
                raise WorkspaceError(f"工作目录已存在，拒绝覆盖: {root}（如确认要新建请用 --replace）")
            shutil.rmtree(root)
        (root / "imports").mkdir(parents=True)
        (root / "exports").mkdir()
        created = now or datetime.now().astimezone()
        state = default_state(name, created, budget_total_fen, str(inputs_dir))
        workspace = cls(root=root, state=state, inputs_dir=inputs_dir)
        # 初始来源指纹，便于日后判断来源是否被改动
        workspace.state["workspace"]["source_fingerprint"] = workspace.fingerprint_sources()
        workspace.save()
        workspace.append_event("workspace.created", created, {
            "name": name,
            "budget_total_fen": budget_total_fen,
            "inputs_dir": str(inputs_dir),
            "source_fingerprint": workspace.state["workspace"]["source_fingerprint"],
        })
        return workspace

    @classmethod
    def open(cls, root: Path, *, inputs_dir: Path | None = None) -> "Workspace":
        root = Path(root).resolve()
        if not root.is_dir():
            raise WorkspaceError(f"工作目录不存在: {root}")
        state_path = root / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkspaceError(f"不是有效的工作目录（缺少 state.json）: {root}") from exc
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"状态文件损坏: {state_path}: {exc}") from exc
        if state.get("state_version") != STATE_VERSION:
            raise WorkspaceError("状态版本不兼容，请使用匹配版本的应用")
        if inputs_dir is None:
            inputs_dir = Path(state["workspace"].get("inputs_dir", ""))
            if not inputs_dir:
                raise WorkspaceError("工作目录未记录来源目录，请用 --inputs 指定")
        return cls(root=root, state=state, inputs_dir=Path(inputs_dir).resolve())

    # ---- 指纹与锁 --------------------------------------------------
    def fingerprint_sources(self) -> str:
        digest = hashlib.sha256()
        for name in sorted([f"{dataset}.json" for dataset in DATASETS] + ["schema.json"]):
            path = self.inputs_dir / name
            if path.exists():
                digest.update(name.encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """跨进程排他锁；进入时重新读取磁盘状态，避免覆盖其他进程的写入。"""
        lock_path = self.root / ".lock"
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                self.reload()
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def reload(self) -> None:
        state_path = self.root / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkspaceError(f"状态文件缺失: {state_path}") from exc
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"状态文件损坏: {state_path}: {exc}") from exc
        if state.get("state_version") != STATE_VERSION:
            raise WorkspaceError("状态版本不兼容")
        self.state = state

    # ---- 状态保存 --------------------------------------------------
    def save(self) -> None:
        payload = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True)
        fd, temp_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.root / "state.json")
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    # ---- 审计 ------------------------------------------------------
    def append_event(self, event_type: str, at: datetime, payload: dict[str, Any]) -> dict[str, Any]:
        log_path = self.root / "audit.jsonl"
        previous = "0" * 64
        seq = 0
        if log_path.exists():
            with log_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise WorkspaceError("审计日志损坏，拒绝继续追加") from exc
                    previous = last.get("hash", previous)
                    seq = int(last.get("seq", seq))
        event = {
            "seq": seq + 1,
            "event": event_type,
            "at": format_ts(at),
            "payload": payload,
            "prev": previous,
        }
        event["hash"] = hash_event(event, previous)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def read_audit(self) -> list[dict[str, Any]]:
        log_path = self.root / "audit.jsonl"
        if not log_path.exists():
            return []
        events = []
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def verify_audit(self) -> list[str]:
        problems: list[str] = []
        previous = "0" * 64
        expected_seq = 1
        for event in self.read_audit():
            body = {key: value for key, value in event.items() if key != "hash"}
            if event.get("prev") != previous:
                problems.append(f"事件 {event.get('seq')} 的前序哈希不匹配")
            if event.get("seq") != expected_seq:
                problems.append(f"事件序号不连续：期望 {expected_seq}，实际 {event.get('seq')}")
            if hash_event(body, previous) != event.get("hash"):
                problems.append(f"事件 {event.get('seq')} 哈希不匹配")
            previous = event.get("hash", "")
            expected_seq += 1
        return problems

    # ---- 导入 ------------------------------------------------------
    def imports(self) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        extra: dict[str, list[dict[str, Any]]] = {dataset: [] for dataset in DATASETS}
        meta: list[dict[str, Any]] = []
        base = self.root / "imports"
        if not base.exists():
            return extra, meta
        for dataset in DATASETS:
            folder = base / dataset
            if not folder.exists():
                continue
            for path in sorted(folder.glob("*.json")):
                rows = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(rows, list):
                    raise WorkspaceError(f"导入文件损坏: {path}")
                extra[dataset].extend(rows)
                meta.append({
                    "dataset": dataset,
                    "file": str(path.relative_to(self.root)),
                    "rows": len(rows),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
        return extra, meta

    def add_import(self, dataset: str, rows: list[dict[str, Any]], at: datetime) -> dict[str, Any]:
        if dataset not in DATASETS:
            raise WorkspaceError(f"未知数据集: {dataset}")
        payload = json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        folder = self.root / "imports" / dataset
        folder.mkdir(parents=True, exist_ok=True)
        stamp = at.astimezone().strftime("%Y%m%dT%H%M%S")
        path = folder / f"{stamp}-{digest}.json"
        if path.exists():
            return {"dataset": dataset, "file": str(path.relative_to(self.root)),
                    "rows": len(rows), "sha256": digest, "duplicate_import": True}
        fd, temp_name = tempfile.mkstemp(prefix=".import-", suffix=".json", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return {"dataset": dataset, "file": str(path.relative_to(self.root)),
                "rows": len(rows), "sha256": digest, "duplicate_import": False}

    # ---- 来源装载 --------------------------------------------------
    def load_source(self, *, verify_manifest: bool = True) -> SourceData:
        extra, meta = self.imports()
        data = load_source(
            self.inputs_dir,
            extra_records=extra,
            import_meta=meta + self.state.get("import_history", []),
            verify_manifest=verify_manifest,
        )
        return data

    # ---- 预算辅助 --------------------------------------------------
    @property
    def budget_total_fen(self) -> int:
        return int(self.state["workspace"]["budget_total_fen"])

    def next_id(self, kind: str, prefix: str, width: int = 3) -> str:
        counters = self.state["counters"]
        counters[kind] = int(counters.get(kind, 0)) + 1
        return f"{prefix}{counters[kind]:0{width}d}"

    def now(self, value: str | None) -> datetime:
        if value is None:
            return datetime.now().astimezone()
        return parse_ts(value, field="当前时间")


def resolve_workspace(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()
