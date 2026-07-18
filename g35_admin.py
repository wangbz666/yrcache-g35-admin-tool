from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_DELETE_IF_UNCHANGED = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('ZREM', KEYS[2], KEYS[1])
    return 1
end
return 0
"""


@dataclass(frozen=True)
class RedisSettings:
    host: str
    port: int
    db: int
    password: str | None
    pin_key: str
    socket_timeout_s: float
    connect_timeout_s: float


@dataclass(frozen=True)
class RedisRecord:
    key: bytes
    raw_value: bytes
    file_path: str
    offset: int
    real_size: int
    alloc_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.decode("utf-8", errors="replace"),
            "file_path": self.file_path,
            "offset": self.offset,
            "real_size": self.real_size,
            "alloc_size": self.alloc_size,
        }


@dataclass(frozen=True)
class ScanResult:
    scanned_keys: int
    records: tuple[RedisRecord, ...]


@dataclass(frozen=True)
class G35Settings:
    mount_path: Path
    cluster_id: str
    osd_ids: tuple[int, ...]


class SharedValueDecodeError(ValueError):
    pass


def _read(fmt: str, raw: bytes, pos: int) -> tuple[Any, int]:
    size = struct.calcsize("<" + fmt)
    if pos + size > len(raw):
        raise SharedValueDecodeError("SharedValueMeta 数据不完整")
    return struct.unpack_from("<" + fmt, raw, pos)[0], pos + size


def decode_shared_file_location(raw: bytes) -> tuple[str, int, int, int]:
    pos = 0
    shape_size, pos = _read("I", raw, pos)
    shape_bytes = shape_size * struct.calcsize("<q")
    if pos + shape_bytes > len(raw):
        raise SharedValueDecodeError("SharedValueMeta shape 越界")
    pos += shape_bytes

    _, pos = _read("b", raw, pos)
    _, pos = _read("i", raw, pos)
    _, pos = _read("i", raw, pos)
    path_size, pos = _read("I", raw, pos)
    if pos + path_size > len(raw):
        raise SharedValueDecodeError("SharedValueMeta 文件路径越界")
    try:
        file_path = raw[pos : pos + path_size].decode("utf-8")
    except UnicodeDecodeError as error:
        raise SharedValueDecodeError("SharedValueMeta 文件路径不是 UTF-8") from error
    pos += path_size

    offset, pos = _read("Q", raw, pos)
    real_size, pos = _read("I", raw, pos)
    alloc_size, pos = _read("I", raw, pos)
    _, pos = _read("Q", raw, pos)
    _, pos = _read("?", raw, pos)
    _, pos = _read("i", raw, pos)
    _, pos = _read("?", raw, pos)
    if pos != len(raw):
        raise SharedValueDecodeError("SharedValueMeta 存在多余数据")
    return file_path, offset, real_size, alloc_size


def load_manifest(path: str | Path) -> frozenset[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    files = data.get("files") if isinstance(data, dict) else data
    if not isinstance(files, list) or not files:
        raise ValueError("文件清单必须是非空 JSON 数组，或包含非空 files 数组")
    if any(not isinstance(item, str) or not item for item in files):
        raise ValueError("文件清单只能包含非空字符串")
    return frozenset(files)


def parse_osd_ids_from_filename(path: str | Path) -> tuple[int, ...] | None:
    filename = Path(path).name
    marker = "_osds_"
    marker_pos = filename.rfind(marker)
    if marker_pos < 0:
        return None
    encoded = filename[marker_pos + len(marker) :].split(".", 1)[0]
    if not encoded:
        return None
    parts = encoded.split("-")
    if any(not part.isdecimal() for part in parts):
        return None
    osd_ids = tuple(int(part) for part in parts)
    if len(set(osd_ids)) != len(osd_ids) or any(osd_id > 0xFFFFFFFF for osd_id in osd_ids):
        return None
    return osd_ids


def list_files(mount_path: Path, osd_id: int) -> tuple[str, ...]:
    if not mount_path.is_dir():
        raise ValueError(f"YRFS 挂载点不存在或不是目录: {mount_path}")
    files = []
    for path in mount_path.rglob("*"):
        if not path.is_file():
            continue
        osd_ids = parse_osd_ids_from_filename(path.name)
        if osd_ids is not None and osd_id in osd_ids:
            files.append(path.relative_to(mount_path).as_posix())
    return tuple(sorted(files))


def delete_files(mount_path: Path, file_paths: Iterable[str], execute: bool) -> dict[str, Any]:
    root = mount_path.resolve()
    details = []
    deleted = 0
    missing = 0
    for relative in sorted(file_paths):
        if Path(relative).is_absolute():
            raise ValueError(f"文件清单包含绝对路径: {relative}")
        absolute = (root / relative).resolve(strict=False)
        if absolute == root or root not in absolute.parents:
            raise ValueError(f"文件清单路径越过 YRFS 挂载点: {relative}")
        exists = absolute.exists()
        status = "would_delete" if exists else "missing"
        if execute and exists:
            if not absolute.is_file():
                raise ValueError(f"文件清单项不是普通文件: {relative}")
            absolute.unlink()
            status = "deleted"
            deleted += 1
        elif not exists:
            missing += 1
        details.append({"file": relative, "status": status})
    return {
        "mode": "execute" if execute else "dry-run",
        "matched": len(details),
        "deleted": deleted,
        "missing": missing,
        "files": details,
    }


def _probe_path(settings: G35Settings, osd_id: int) -> Path:
    return settings.mount_path / ".yrcache_g35_probes" / settings.cluster_id / f"probe_osd_{osd_id}.dat"


def _yrfs_ops() -> tuple[Any, Any]:
    try:
        from yrcache import c_ops
    except ImportError as error:
        raise RuntimeError("探活文件命令需要已编译的 yrcache.c_ops") from error
    return c_ops._g35_query_yrfs_file_osds, c_ops._g35_probe_payload


def _log(verbose: bool, message: str) -> None:
    if verbose:
        print(f"yrcache-g35-admin: {message}", file=sys.stderr)


def _yrcli_create_command(path: Path, osd_id: int) -> list[str]:
    return [
        "yrcli",
        "--create",
        "--stripesize=1m",
        "--stripecount=1",
        "--pool=default",
        str(path),
        f"--owners={osd_id}",
    ]


def _create_probe_with_yrcli(path: Path, osd_id: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _yrcli_create_command(path, osd_id),
        capture_output=True,
        text=True,
        check=False,
    )


def verify_probe(settings: G35Settings, osd_id: int) -> dict[str, Any]:
    query_layout, build_payload = _yrfs_ops()
    path = _probe_path(settings, osd_id)
    status, actual_osds = query_layout(str(path))
    if int(status) != 0:
        return {"osd_id": osd_id, "path": str(path), "status": "layout_error", "error_code": int(status)}
    if list(actual_osds) != [osd_id]:
        return {"osd_id": osd_id, "path": str(path), "status": "wrong_layout", "actual_osds": list(actual_osds)}
    try:
        actual = path.read_bytes()
    except OSError as error:
        return {"osd_id": osd_id, "path": str(path), "status": "read_error", "error": str(error)}
    if actual != build_payload(settings.cluster_id, osd_id):
        return {"osd_id": osd_id, "path": str(path), "status": "content_error"}
    return {"osd_id": osd_id, "path": str(path), "status": "ok"}


def create_probes(
    settings: G35Settings, execute: bool, overwrite: bool, verbose: bool = False
) -> dict[str, Any]:
    build_payload = _yrfs_ops()[1] if execute else None
    details: list[dict[str, Any]] = []
    created = 0
    skipped = 0
    for osd_id in settings.osd_ids:
        path = _probe_path(settings, osd_id)
        exists = path.exists()
        entry: dict[str, Any] = {"osd_id": osd_id, "path": str(path)}
        _log(verbose, f"[osd={osd_id}] 开始处理 path={path} exists={exists}")
        if not execute:
            if exists and not overwrite:
                entry["status"] = "would_skip_exists"
                skipped += 1
            else:
                entry["status"] = "would_overwrite" if exists else "would_create"
            _log(verbose, f"[osd={osd_id}] dry-run status={entry['status']}")
            details.append(entry)
            continue
        if exists and not overwrite:
            entry["status"] = "skipped_exists"
            skipped += 1
            _log(verbose, f"[osd={osd_id}] 跳过已存在文件")
            details.append(entry)
            continue
        _log(verbose, f"[osd={osd_id}] 步骤1/4: 生成 payload")
        payload = build_payload(settings.cluster_id, osd_id)
        _log(verbose, f"[osd={osd_id}] payload bytes={len(payload)}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _log(verbose, f"[osd={osd_id}] 删除已存在文件以便覆盖")
            path.unlink()
        command = _yrcli_create_command(path, osd_id)
        _log(verbose, f"[osd={osd_id}] 步骤2/4: 执行 {' '.join(command)}")
        try:
            result = _create_probe_with_yrcli(path, osd_id)
        except FileNotFoundError:
            error_msg = "找不到 yrcli 命令，请确认已安装并在 PATH 中"
            entry["status"] = "yrcli_error"
            entry["error"] = error_msg
            created += 1
            _log(verbose, f"[osd={osd_id}] 失败 status=yrcli_error error={error_msg}")
            details.append(entry)
            continue
        if result.stdout and result.stdout.strip():
            _log(verbose, f"[osd={osd_id}] yrcli stdout:\n{result.stdout.rstrip()}")
        if result.stderr and result.stderr.strip():
            _log(verbose, f"[osd={osd_id}] yrcli stderr:\n{result.stderr.rstrip()}")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            error_msg = (
                f"yrcli 创建探活文件失败: {path} (exit={result.returncode}"
                + (f", {detail}" if detail else "")
                + ")"
            )
            entry["status"] = "yrcli_error"
            entry["error"] = error_msg
            created += 1
            _log(verbose, f"[osd={osd_id}] 失败 status=yrcli_error error={error_msg}")
            details.append(entry)
            continue
        _log(verbose, f"[osd={osd_id}] yrcli 成功 exit={result.returncode}")
        _log(verbose, f"[osd={osd_id}] 步骤3/4: 写入 payload")
        path.write_bytes(payload)
        _log(verbose, f"[osd={osd_id}] 步骤4/4: 校验布局与内容")
        verify = verify_probe(settings, osd_id)
        entry["bytes"] = len(payload)
        entry["verify"] = verify["status"]
        entry.update({k: v for k, v in verify.items() if k not in {"osd_id", "path", "status"}})
        entry["status"] = "created" if verify["status"] == "ok" else f"created_but_{verify['status']}"
        created += 1
        extra = {k: v for k, v in entry.items() if k not in {"osd_id", "path", "status", "bytes", "verify"}}
        _log(
            verbose,
            f"[osd={osd_id}] 完成 status={entry['status']} verify={verify['status']}"
            + (f" detail={extra}" if extra else ""),
        )
        details.append(entry)
    return {
        "mode": "execute" if execute else "dry-run",
        "cluster_id": settings.cluster_id,
        "mount_path": str(settings.mount_path),
        "matched": len(details),
        "created": created,
        "skipped": skipped,
        "probes": details,
    }


def _as_bytes(value: bytes | str) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _looks_like_yrcache_key(key: bytes) -> bool:
    try:
        parts = key.decode("utf-8").split(":", 4)
        if len(parts) != 5:
            return False
        int(parts[0])
        int(parts[1])
        int(parts[2])
        return True
    except (UnicodeDecodeError, ValueError):
        return False


def scan_redis_records(
    client: Any,
    file_paths: frozenset[str],
    pin_key: str,
    scan_batch: int = 1000,
) -> ScanResult:
    if scan_batch <= 0:
        raise ValueError("scan_batch 必须大于 0")

    cursor = 0
    scanned_keys = 0
    records: list[RedisRecord] = []
    pin_key_bytes = pin_key.encode("utf-8")
    while True:
        cursor, keys = client.scan(cursor=cursor, match="*", count=scan_batch)
        scanned_keys += len(keys)
        candidates = []
        for key in keys:
            key = _as_bytes(key)
            if not key.startswith(b"yrcache:g35:") and key != pin_key_bytes and _looks_like_yrcache_key(key):
                candidates.append(key)
        if candidates:
            pipeline = client.pipeline(transaction=False)
            for key in candidates:
                pipeline.type(key)
            types = pipeline.execute()
            string_keys = [key for key, redis_type in zip(candidates, types) if _as_bytes(redis_type) == b"string"]

            pipeline = client.pipeline(transaction=False)
            for key in string_keys:
                pipeline.get(key)
            values = pipeline.execute()
            for key, raw_value in zip(string_keys, values):
                if raw_value is None:
                    continue
                raw_value = _as_bytes(raw_value)
                try:
                    file_path, offset, real_size, alloc_size = decode_shared_file_location(raw_value)
                except SharedValueDecodeError as error:
                    raise SharedValueDecodeError(
                        f"YRCache Redis 记录无法反序列化: {key.decode('utf-8', errors='replace')}"
                    ) from error
                if file_path in file_paths:
                    records.append(
                        RedisRecord(
                            key=key,
                            raw_value=raw_value,
                            file_path=file_path,
                            offset=offset,
                            real_size=real_size,
                            alloc_size=alloc_size,
                        )
                    )
        if int(cursor) == 0:
            break
    return ScanResult(scanned_keys=scanned_keys, records=tuple(records))


def delete_redis_records(client: Any, records: Iterable[RedisRecord], pin_key: str) -> dict[str, Any]:
    deleted = 0
    changed = 0
    details: list[dict[str, str]] = []
    for record in records:
        result = client.eval(_DELETE_IF_UNCHANGED, 2, record.key, pin_key, record.raw_value)
        status = "deleted" if int(result) == 1 else "changed_or_missing"
        deleted += int(result) == 1
        changed += int(result) != 1
        details.append({"key": record.key.decode("utf-8", errors="replace"), "status": status})
    return {"deleted": deleted, "changed_or_missing": changed, "records": details}


def _load_shared_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError("使用 --config 需要安装 PyYAML") from error
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    shared = config.get("shared_storage_cache_config", {})
    if not isinstance(shared, dict):
        raise ValueError("shared_storage_cache_config 必须是对象")
    return shared


def _load_settings(args: argparse.Namespace, shared: dict[str, Any] | None = None) -> RedisSettings:
    shared = _load_shared_config(args.config) if shared is None else shared

    host = getattr(args, "redis_host", None) or shared.get("redis_host")
    if not host:
        raise ValueError("必须通过 --config 或 --redis-host 指定 Redis 地址")
    connect_timeout_ms = int(shared.get("redis_connect_timeout_ms", 1000))
    socket_timeout_ms = int(shared.get("redis_socket_timeout_ms", 3000))
    return RedisSettings(
        host=str(host),
        port=int(args.redis_port if getattr(args, "redis_port", None) is not None else shared.get("redis_port", 6379)),
        db=int(args.redis_db if getattr(args, "redis_db", None) is not None else shared.get("redis_db", 0)),
        password=args.redis_password or os.environ.get("YRCACHE_REDIS_PASSWORD"),
        pin_key=str(getattr(args, "redis_pin_key", None) or shared.get("redis_pin_key", "share_fs_gc_zset")),
        socket_timeout_s=socket_timeout_ms / 1000.0,
        connect_timeout_s=connect_timeout_ms / 1000.0,
    )


def _load_g35_settings(args: argparse.Namespace, shared: dict[str, Any] | None = None) -> G35Settings:
    shared = _load_shared_config(args.config) if shared is None else shared
    mount_path = shared.get("mount_path")
    cluster_id = str(shared.get("g35_cluster_id", "")).strip()
    osd_ids = shared.get("g35_osd_ids") or []
    if not mount_path or not cluster_id or not isinstance(osd_ids, list) or not osd_ids:
        raise ValueError("配置必须包含 mount_path、g35_cluster_id 和非空 g35_osd_ids")
    if any(not isinstance(osd_id, int) or osd_id < 0 or osd_id > 0xFFFFFFFF for osd_id in osd_ids):
        raise ValueError("g35_osd_ids 必须是无符号 32 位整数数组")
    if len(osd_ids) != len(set(osd_ids)):
        raise ValueError("g35_osd_ids 不能重复")
    mount = Path(str(mount_path))
    if not mount.is_dir():
        raise ValueError(f"YRFS 挂载点不存在或不是目录: {mount}")
    return G35Settings(mount, cluster_id, tuple(osd_ids))


def _connect(settings: RedisSettings) -> Any:
    try:
        import redis
    except ImportError as error:
        raise RuntimeError("需要安装 redis Python 包") from error
    client = redis.Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        password=settings.password,
        socket_timeout=settings.socket_timeout_s,
        socket_connect_timeout=settings.connect_timeout_s,
        decode_responses=False,
    )
    client.ping()
    return client


def _write_report(report: dict[str, Any], output: str | None) -> None:
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(content, encoding="utf-8")
    else:
        sys.stdout.write(content)


def _add_redis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="YRCache YAML 配置文件")
    parser.add_argument("--redis-host")
    parser.add_argument("--redis-port", type=int)
    parser.add_argument("--redis-db", type=int)
    parser.add_argument("--redis-password")
    parser.add_argument("--redis-pin-key")
    parser.add_argument("--manifest", required=True, help="受影响文件 JSON 清单")
    parser.add_argument("--scan-batch", type=int, default=1000)


def _add_mode_options(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只显示将要执行的操作，默认行为")
    mode.add_argument("--execute", action="store_true", help="实际执行操作")


def _add_config_redis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="YRCache YAML 配置文件")
    parser.add_argument("--redis-password")
    parser.add_argument("--scan-batch", type=int, default=1000)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="yrcache-g35-admin", description="YRCache G3.5 运维工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-redis-records", help="查询指向受影响文件的 Redis 记录")
    _add_redis_options(list_parser)
    list_parser.add_argument("--output")
    list_parser.add_argument("--count-only", action="store_true")

    delete_parser = subparsers.add_parser(
        "delete-redis-records",
        help="条件删除指向受影响文件的 Redis 记录",
    )
    _add_redis_options(delete_parser)
    _add_mode_options(delete_parser)
    delete_parser.add_argument("--report")

    files_parser = subparsers.add_parser("list-files", help="生成或检查受影响文件清单")
    files_parser.add_argument("--config", required=True, help="YRCache YAML 配置文件")
    source = files_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--osd-id", type=int)
    source.add_argument("--manifest")
    files_parser.add_argument("--summary", action="store_true")
    files_parser.add_argument("--output")

    delete_files_parser = subparsers.add_parser("delete-files", help="删除受影响的 YRFS 数据文件")
    _add_config_redis_options(delete_files_parser)
    delete_files_parser.add_argument("--manifest", required=True)
    delete_files_parser.add_argument("--require-no-redis-references", action="store_true")
    _add_mode_options(delete_files_parser)
    delete_files_parser.add_argument("--report")

    create_probes_parser = subparsers.add_parser(
        "create-probes",
        help="为配置中的每个 OSD 创建探活文件并写入校验内容",
    )
    create_probes_parser.add_argument("--config", required=True, help="YRCache YAML 配置文件")
    create_probes_parser.add_argument(
        "--overwrite", action="store_true", help="覆盖已存在的探活文件，默认跳过"
    )
    create_probes_parser.add_argument(
        "--verbose", action="store_true", help="输出每个 OSD 的分步执行日志到 stderr"
    )
    _add_mode_options(create_probes_parser)
    create_probes_parser.add_argument("--report")

    verify_parser = subparsers.add_parser("verify-recovery", help="验证 OSD 恢复的静态条件")
    _add_config_redis_options(verify_parser)
    verify_parser.add_argument("--osd-id", type=int, required=True)
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--ack-runtime-checks", action="store_true")
    verify_parser.add_argument("--report")
    return parser.parse_args(argv)


def _handle_list_files(args: argparse.Namespace) -> int:
    g35 = _load_g35_settings(args)
    if args.manifest:
        files = load_manifest(args.manifest)
        report = {
            "mount_path": str(g35.mount_path),
            "files": len(files),
            "existing": sum((g35.mount_path / file).is_file() for file in files),
        }
    else:
        if args.osd_id not in g35.osd_ids:
            raise ValueError(f"OSD {args.osd_id} 不在 g35_osd_ids 中")
        files = list_files(g35.mount_path, args.osd_id)
        report = {"mount_path": str(g35.mount_path), "osd_id": args.osd_id, "files": list(files)}
    _write_report(report, args.output)
    return 0


def _handle_create_probes(args: argparse.Namespace) -> int:
    g35 = _load_g35_settings(args)
    _write_report(create_probes(g35, args.execute, args.overwrite, args.verbose), args.report)
    return 0


def _scan_command_records(
    args: argparse.Namespace, shared: dict[str, Any], file_paths: frozenset[str]
) -> tuple[RedisSettings | None, Any, ScanResult]:
    needs_redis = args.command in {"list-redis-records", "delete-redis-records", "verify-recovery"} or \
        args.require_no_redis_references
    if not needs_redis:
        return None, None, ScanResult(scanned_keys=0, records=())
    settings = _load_settings(args, shared)
    client = _connect(settings)
    return settings, client, scan_redis_records(client, file_paths, settings.pin_key, args.scan_batch)


def _handle_redis_command(
    args: argparse.Namespace, settings: RedisSettings, client: Any, result: ScanResult
) -> int:
    records = [record.to_dict() for record in result.records]
    if args.command == "list-redis-records":
        report = {"scanned_keys": result.scanned_keys, "matched": len(records)}
        if not args.count_only:
            report["records"] = records
        _write_report(report, args.output)
        return 0
    if not args.execute:
        _write_report(
            {"mode": "dry-run", "scanned_keys": result.scanned_keys,
             "would_delete": len(records), "records": records},
            args.report,
        )
        return 0
    report = delete_redis_records(client, result.records, settings.pin_key)
    report.update({"mode": "execute", "scanned_keys": result.scanned_keys, "matched": len(records)})
    _write_report(report, args.report)
    return 0


def _handle_delete_files(
    args: argparse.Namespace, g35: G35Settings, file_paths: frozenset[str], result: ScanResult
) -> int:
    _require_no_redis_references(args, result, "删除文件")
    _write_report(delete_files(g35.mount_path, file_paths, args.execute), args.report)
    return 0


def _require_no_redis_references(args: argparse.Namespace, result: ScanResult, operation: str) -> None:
    if args.execute and not args.require_no_redis_references:
        raise ValueError(f"实际{operation}必须指定 --require-no-redis-references")
    if args.require_no_redis_references and result.records:
        raise RuntimeError(f"仍有 {len(result.records)} 条 Redis 记录引用清单中的文件")


def _handle_verify_recovery(
    args: argparse.Namespace, g35: G35Settings, existing_files: list[str], result: ScanResult
) -> int:
    records = [record.to_dict() for record in result.records]
    probes = [verify_probe(g35, osd_id) for osd_id in g35.osd_ids]
    static_checks_ok = not existing_files and not records and all(probe["status"] == "ok" for probe in probes)
    ok = static_checks_ok and args.ack_runtime_checks
    report = {
        "ok": ok,
        "static_checks_ok": static_checks_ok,
        "runtime_checks_acknowledged": args.ack_runtime_checks,
        "remaining_files": existing_files,
        "remaining_redis_records": records,
        "probes": probes,
        "runtime_checks_required": [
            "确认各 YRCache 实例已经移除目标 OSD 的异常标记",
            "确认文件访问模式为 CLOSED，或因其他异常 OSD 保持 DEGRADED",
            "确认原受影响 KV Cache 查询返回未命中并可重新计算",
        ],
    }
    _write_report(report, args.report)
    return 0 if ok else 2


def _execute(args: argparse.Namespace) -> int:
    if args.command == "list-files":
        return _handle_list_files(args)
    if args.command == "create-probes":
        return _handle_create_probes(args)
    shared = _load_shared_config(args.config)
    file_paths = load_manifest(args.manifest)
    settings, client, result = _scan_command_records(args, shared, file_paths)
    if args.command in {"list-redis-records", "delete-redis-records"}:
        return _handle_redis_command(args, settings, client, result)
    g35 = _load_g35_settings(args, shared)
    if args.command == "delete-files":
        return _handle_delete_files(args, g35, file_paths, result)
    if args.osd_id not in g35.osd_ids:
        raise ValueError(f"OSD {args.osd_id} 不在 g35_osd_ids 中")
    existing_files = sorted(file for file in file_paths if (g35.mount_path / file).exists())
    return _handle_verify_recovery(args, g35, existing_files, result)


def main(argv: list[str] | None = None) -> int:
    try:
        return _execute(parse_args(argv))
    except Exception as error:
        print(f"yrcache-g35-admin: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
