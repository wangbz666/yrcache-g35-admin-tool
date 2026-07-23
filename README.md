# YRCache G3.5 Admin Tool

`g35_admin.py` 是一个面向 YRCache G3.5 运维场景的单文件命令行工具。它用于处理 OSD 故障、更换、清理和恢复验证流程，包含受影响文件扫描、Redis 元数据查询与删除、YRFS 数据文件删除、探活文件创建，以及恢复状态校验。

本项目只管理一个脚本：

```text
g35_admin.py
```

## 适用场景

当 G3.5 的某个 OSD 故障或换盘后，需要清理所有落在该 OSD 上的旧缓存数据，避免 Redis 命中后读取到不存在或不可用的 YRFS 文件。本工具按安全顺序执行：

1. 扫描数据目录，生成受影响文件清单。
2. 检查清单规模与文件是否仍存在。
3. 查询 Redis 中指向这些文件的记录。
4. 先删除 Redis 记录。
5. 再删除 YRFS 数据文件。
6. 重建或创建探活文件。
7. 验证指定故障 OSD 的受影响文件与 Redis 是否已清理完成。

重要原则：必须先删 Redis 记录，再删数据文件。反向操作会产生“Redis 命中但文件已不存在”的窗口。

## 运行方式

当前项目没有打包入口，直接用 Python 执行脚本：

```bash
python g35_admin.py --help
python g35_admin.py <command> --help
python g35_admin.py <command> [options]
```

如果你已经把它安装成 console script，也可以用 `yrcache-g35-admin`，但本项目默认以 `python g35_admin.py` 为准。

建议使用 Python 3.8+。脚本使用了类型标注语法和 `from __future__ import annotations`，过旧 Python 版本可能无法运行。

## 依赖

基础功能需要：

```bash
pip install redis PyYAML
```

探活文件校验和创建还需要：

1. 已编译的 `yrcache.c_ops`，并暴露：
   - `_g35_query_yrfs_file_osds(path)`：查询 YRFS 文件实际分布在哪些 OSD 上。
   - `_g35_probe_payload(cluster_id, osd_id)`：生成探活文件内容。
2. 可执行的 `yrcli`（`create-probes --execute` 会调用它创建带布局的文件）。

`create-probes --dry-run` 不加载 `c_ops`，只预览路径；`--execute` 会调用 `yrcli --create`，再加载 `c_ops` 写入内容并校验。

## 配置文件

工具读取 YAML 中的 `shared_storage_cache_config`：

```yaml
shared_storage_cache_config:
  mount_path: "/mnt/real-yrfs"
  data_sub_path: "/yrcache_01"
  g35_cluster_id: "wbz-test-cluster"
  g35_osd_ids: [101, 102, 103, 104]

  redis_host: "127.0.0.1"
  redis_port: 6379
  redis_db: 0
  redis_pin_key: "share_fs_gc_zset"
  redis_connect_timeout_ms: 1000
  redis_socket_timeout_ms: 3000
```

字段说明：

| 字段 | 必填 | 用途 |
| --- | --- | --- |
| `mount_path` | 是 | YRFS 挂载点。探活路径、删除路径校验都基于它。 |
| `data_sub_path` | 是 | 相对 `mount_path` 的业务数据子目录。`list-files` 只扫描 `mount_path + data_sub_path`。 |
| `g35_cluster_id` | 是 | G3.5 集群 ID，用于探活路径和探活内容。 |
| `g35_osd_ids` | 是 | 当前集群有效 OSD ID 列表，必须是非重复无符号 32 位整数。 |
| `redis_host` | Redis 相关命令必填 | Redis 地址。也可通过 `--redis-host` 覆盖。 |
| `redis_port` | 否 | Redis 端口，默认 `6379`。 |
| `redis_db` | 否 | Redis DB，默认 `0`。 |
| `redis_pin_key` | 否 | Redis pin zset key，默认 `share_fs_gc_zset`。 |
| `redis_connect_timeout_ms` | 否 | Redis 连接超时，默认 `1000`。 |
| `redis_socket_timeout_ms` | 否 | Redis 读写超时，默认 `3000`。 |

Redis 密码可以通过命令参数或环境变量传入：

```bash
export YRCACHE_REDIS_PASSWORD='your-password'
```

命令参数 `--redis-password` 优先级高于环境变量。

## 命令总览

```bash
python g35_admin.py list-files
python g35_admin.py list-redis-records
python g35_admin.py delete-redis-records
python g35_admin.py delete-files
python g35_admin.py create-probes
python g35_admin.py verify-recovery
```

### 通用约定

- 会改变数据的命令支持 `--dry-run` / `--execute`：
  - 不指定 `--execute` 时默认是 dry-run（只预览）。
  - `--dry-run` 与 `--execute` 互斥。

### 结果输出约定

`--report` 与 `--output` 底层实现相同，但语义不同：

| 参数 | 必填 | 用途 |
| --- | --- | --- |
| `--report` | 是（**仅** `list-files --osd-id`） | 写入**下游命令会依赖**的清单文件，供后续 `--manifest` 读取。 |
| `--output` | 否 | 可选保存本次 JSON 记录，仅供查看或留档，**不会被其他命令读取**。 |

典型流水线：

```text
list-files --osd-id --report affected-files.json   # 生成清单（--report 必填）
        ↓
list-files --manifest affected-files.json          # 检查清单（只需 --manifest，--output 可选）
delete-redis-records --manifest affected-files.json
delete-files --manifest affected-files.json
...
```

- `list-files --manifest` **读取**第 1 步 `--report` 生成的文件，**不需要** `--report`。
- 其他命令也只用 `--manifest` 读取清单，用可选 `--output` 留档。
- 不指定 `--output` 时，JSON 打印到终端 stdout。
- `create-probes --verbose` 的分步日志打到 stderr，不与 JSON 混写。
- 涉及 OSD 的参数统一支持 `--osd-id` 与 `--osd_id`：`list-files`、`create-probes`。

---

## 1. 生成受影响文件清单

命令：`list-files --osd-id`

扫描数据目录，找出文件名中包含目标 OSD 的业务数据文件，生成后续清理用的清单。

### 1.1 配置示例

```yaml
shared_storage_cache_config:
  mount_path: "/mnt/real-yrfs"
  data_sub_path: "/yrcache_01"
  g35_cluster_id: "wbz-test-cluster"
  g35_osd_ids: [101, 102, 103, 104]
```

本功能需要：`mount_path`、`data_sub_path`、`g35_cluster_id`、`g35_osd_ids`。不需要 Redis 配置。

### 1.2 执行方式

```bash
export YRCACHE_CONFIG=/etc/yrcache/yrcache.yaml
export FAILED_OSD=306
export WORK_DIR=/var/tmp/yrcache-g35-osd-${FAILED_OSD}
mkdir -p "$WORK_DIR"

python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --osd-id "$FAILED_OSD" \
  --report "$WORK_DIR/affected-files.json"
```

参数说明：

| 参数 | 必填 | 作用 / 不给会怎样 |
| --- | --- | --- |
| `--config` | 是 | YAML 配置文件。不给会报参数错误。 |
| `--osd-id` / `--osd_id` | 是（与 `--manifest` 二选一） | 按该 OSD 扫描数据目录。不在 `g35_osd_ids` 中会报错。本模式不要同时给 `--manifest`。 |
| `--report` | 是 | 写入清单 JSON，供后续命令 `--manifest` 使用。不给会报错。 |
| `--output` | 否 | 本模式不可用；若指定会报错。 |
| `--summary` | 否 | 只能与 `--manifest` 一起用；本模式若指定会报错。 |
| `--manifest` | 否 | 本模式不要用；给了就变成“检查清单”。 |

扫描逻辑：

- 只扫 `mount_path + data_sub_path`，例如 `/mnt/real-yrfs/yrcache_01`。
- 不扫探活目录 `.yrcache_g35_probes/`。
- 解析文件名最后一个 `_osds_` 后的 OSD 列表，例如 `_osds_101-102-103.dat`。
- 精确匹配整数，不会把 `17`/`70`/`107` 误判成 `7`。
- 故障 OSD 上 `stat` 可能 EIO：仍按文件名匹配并收录，错误记入 `scan_errors`，扫描不中断。
- 清单路径相对 `mount_path`，例如 `yrcache_01/worker_id_1/cache_a_osds_306-xxx.dat`。

### 1.3 产出报告

```json
{
  "mount_path": "/mnt/real-yrfs",
  "data_path": "/mnt/real-yrfs/yrcache_01",
  "osd_id": 306,
  "files": [
    "yrcache_01/worker_id_1/cache_a_osds_306-307.dat",
    "yrcache_01/worker_id_2/cache_b_osds_305-306.dat"
  ],
  "scan_errors": [
    {
      "path": "yrcache_01/worker_id_1/cache_a_osds_306-307.dat",
      "error": "OSError: [Errno 5] Input/output error",
      "phase": "stat"
    }
  ]
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `mount_path` | YRFS 挂载点。 |
| `data_path` | 实际扫描的数据目录。 |
| `osd_id` | 本次扫描的目标 OSD。 |
| `files` | 受影响文件相对路径列表（相对 `mount_path`）。 |
| `scan_errors` | 扫描中的 I/O / scandir 错误；可能为空数组。 |
| `scan_errors[].path` | 出错路径（相对 `mount_path`）。 |
| `scan_errors[].error` | 错误信息。 |
| `scan_errors[].phase` | `stat`：对条目做 is_file/is_dir 失败；`scandir`：列目录失败。 |

该报告没有 `status` 字段；文件要么出现在 `files` 中，要么因文件名不匹配而不收录。

---

## 2. 检查受影响文件清单

命令：`list-files --manifest`

读取第 1 节 `--report` 生成的清单文件，检查其中每个文件是否仍存在。

- **不需要 `--report`**（那是生成清单时用的）。
- **`--output` 可选**；不给则结果打印到终端。
- **不加 `--summary`**：输出每个文件的 `existing` / `missing` 明细。
- **加 `--summary`**：只输出文件总数和仍存在数。

### 2.1 配置示例

与第 1 节相同，需要 `mount_path`、`data_sub_path`、`g35_cluster_id`、`g35_osd_ids`。

### 2.2 执行方式

明细检查（默认）：

```bash
python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --output "$WORK_DIR/affected-files-check.json"
```

只看摘要：

```bash
python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --summary \
  --output "$WORK_DIR/affected-files-summary.json"
```

参数说明：

| 参数 | 必填 | 作用 / 不给会怎样 |
| --- | --- | --- |
| `--config` | 是 | YAML 配置文件。 |
| `--manifest` | 是（与 `--osd-id` 二选一） | 第 1 节 `--report` 生成的清单文件路径。 |
| `--summary` | 否 | 只输出摘要计数。不给则输出每个文件的存在状态明细。不能与 `--osd-id` 同用。 |
| `--output` | 否 | 可选写入 JSON 记录；**不给则打印到终端**。 |
| `--report` | 否 | 本模式不可用；若指定会报错。 |
| `--osd-id` | 否 | 本模式不要用；给了就变成生成清单。 |

存在性检查对 I/O error 做了容错：访问失败视为文件仍存在（故障 OSD 上常见）。

### 2.3 产出报告

不加 `--summary`（明细）：

```json
{
  "mount_path": "/mnt/real-yrfs",
  "data_path": "/mnt/real-yrfs/yrcache_01",
  "files": 2,
  "existing": 1,
  "missing": 1,
  "details": [
    {
      "file": "yrcache_01/worker_id_1/cache_a_osds_306-307.dat",
      "status": "existing"
    },
    {
      "file": "yrcache_01/worker_id_2/cache_b_osds_305-306.dat",
      "status": "missing"
    }
  ]
}
```

加 `--summary`（摘要）：

```json
{
  "mount_path": "/mnt/real-yrfs",
  "data_path": "/mnt/real-yrfs/yrcache_01",
  "files": 2,
  "existing": 1
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `mount_path` | YRFS 挂载点。 |
| `data_path` | 数据目录。 |
| `files` | 清单中的文件数量。 |
| `existing` | 当前仍存在的数量；I/O error 也计为存在。 |
| `missing` | 已不存在的数量；仅明细模式输出。 |
| `details` | 每个文件的检查结果；仅明细模式输出。 |

`details[].status`：

| status | 含义 |
| --- | --- |
| `existing` | 文件仍存在（含 I/O error 视为存在）。 |
| `missing` | 文件已不存在。 |

清理完成后期望：`existing` 为 `0`，明细中全部为 `missing`。

清单格式示例：

```json
[
  "yrcache_01/worker_id_1/cache_a_osds_306-307.dat"
]
```

或：

```json
{
  "files": [
    "yrcache_01/worker_id_1/cache_a_osds_306-307.dat"
  ]
}
```

---

## 3. 查询 Redis 记录

命令：`list-redis-records`

扫描 Redis，找出 `location.file_path` 指向清单中文件的 YRCache 记录。

### 3.1 配置示例

```yaml
shared_storage_cache_config:
  mount_path: "/mnt/real-yrfs"
  data_sub_path: "/yrcache_01"
  g35_cluster_id: "wbz-test-cluster"
  g35_osd_ids: [101, 102, 103, 104]

  redis_host: "127.0.0.1"
  redis_port: 6379
  redis_db: 0
  redis_pin_key: "share_fs_gc_zset"
  redis_connect_timeout_ms: 1000
  redis_socket_timeout_ms: 3000
```

本功能至少需要能连上 Redis：`redis_host`（配置或 `--redis-host`），以及可选的 port/db/password/pin_key。

### 3.2 执行方式

```bash
python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --scan-batch 1000 \
  --output "$WORK_DIR/affected-redis-records.json"
```

只看数量：

```bash
python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --count-only
```

参数说明：

| 参数 | 必填 | 作用 / 不给会怎样 |
| --- | --- | --- |
| `--manifest` | 是 | 受影响文件清单。不给会报参数错误。 |
| `--config` | 建议 | 从中读 Redis 连接信息。若不用 config，必须用 `--redis-host` 等补齐。 |
| `--redis-host` | 条件必填 | 覆盖/提供 Redis 地址。config 和本参数都没有时会报错。 |
| `--redis-port` | 否 | 覆盖端口；默认配置值或 `6379`。 |
| `--redis-db` | 否 | 覆盖 DB；默认配置值或 `0`。 |
| `--redis-password` | 否 | Redis 密码；优先于 `YRCACHE_REDIS_PASSWORD`。不给则用环境变量或无密码。 |
| `--redis-pin-key` | 否 | 覆盖 pin zset key；默认配置值或 `share_fs_gc_zset`。查询阶段不删 pin，但设置仍会加载。 |
| `--scan-batch` | 否 | Redis `SCAN` 的 `COUNT` 提示，默认 `1000`。越小越平滑，越大单次压力更大。 |
| `--count-only` | 否 | 只输出 `scanned_keys`/`matched`，不含 `records`。不给则输出完整明细。 |
| `--output` | 否 | 写入 JSON 文件；**不给则打印到终端**。 |

实现细节：

- 使用 `SCAN`，不用 `KEYS *`。
- 只处理看起来像 YRCache 的 string key。
- 反序列化 SharedValueMeta，保留 `file_path` 落在清单中的记录。

### 3.3 产出报告

完整输出：

```json
{
  "scanned_keys": 12876,
  "matched": 2,
  "records": [
    {
      "key": "1:2:3:abc:def",
      "file_path": "yrcache_01/worker_id_1/cache_a_osds_306-307.dat",
      "offset": 0,
      "real_size": 4096,
      "alloc_size": 4096
    }
  ]
}
```

`--count-only` 输出：

```json
{
  "scanned_keys": 12876,
  "matched": 2
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `scanned_keys` | SCAN 过程中见到的 key 总数（含未匹配）。 |
| `matched` | 命中清单文件的记录数。 |
| `records` | 明细；`--count-only` 时不出现。 |
| `records[].key` | Redis key。 |
| `records[].file_path` | SharedValueMeta 中的文件路径（相对 `mount_path`）。 |
| `records[].offset` | 文件内偏移。 |
| `records[].real_size` | 实际数据大小。 |
| `records[].alloc_size` | 分配大小。 |

清理 Redis 后期望：`matched` 为 `0`。

---

## 4. 删除 Redis 记录

命令：`delete-redis-records`

先扫描命中清单的 Redis 记录，再按条件删除（value 未变才删，并 `ZREM` pin zset）。

### 4.1 配置示例

与第 3 节相同，需要 Redis 连接信息。

### 4.2 执行方式

预演（推荐先做）：

```bash
python g35_admin.py delete-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --dry-run \
  --output "$WORK_DIR/redis-delete-dry-run.json"
```

实际删除：

```bash
python g35_admin.py delete-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --execute \
  --output "$WORK_DIR/redis-delete-result.json"
```

参数说明：

| 参数 | 必填 | 作用 / 不给会怎样 |
| --- | --- | --- |
| `--manifest` | 是 | 受影响文件清单。 |
| `--config` | 建议 | 读 Redis 连接；也可用 `--redis-host` 等覆盖。 |
| `--redis-host` / `--redis-port` / `--redis-db` / `--redis-password` / `--redis-pin-key` | 否 | 覆盖 Redis 连接与 pin key。无 host 时会报错。 |
| `--scan-batch` | 否 | `SCAN COUNT`，默认 `1000`。 |
| `--dry-run` | 否 | 只预览，不删除。不指定 `--execute` 时默认就是 dry-run。 |
| `--execute` | 否 | 实际条件删除。与 `--dry-run` 互斥。 |
| `--output` | 否 | 可选写入 JSON 操作记录；**不给则打印到终端**。 |

说明：`--dry-run` 与 `list-redis-records` 底层扫描相同，命中集合一致；差异是字段名（`would_delete` vs `matched`），且前者可继续 `--execute`。

删除逻辑（Lua）：

- 仅当当前 value 与扫描时完全一致才 `DEL`。
- 同时从 `redis_pin_key` 做 `ZREM`。
- value 已变或 key 已不存在 → `changed_or_missing`，不强删。

### 4.3 产出报告

dry-run：

```json
{
  "mode": "dry-run",
  "scanned_keys": 12876,
  "would_delete": 2,
  "records": [
    {
      "key": "1:2:3:abc:def",
      "file_path": "yrcache_01/worker_id_1/cache_a_osds_306-307.dat",
      "offset": 0,
      "real_size": 4096,
      "alloc_size": 4096
    }
  ]
}
```

execute：

```json
{
  "mode": "execute",
  "scanned_keys": 12876,
  "matched": 2,
  "deleted": 2,
  "changed_or_missing": 0,
  "records": [
    {
      "key": "1:2:3:abc:def",
      "status": "deleted"
    }
  ]
}
```

字段说明：

| 字段 | 出现时机 | 含义 |
| --- | --- | --- |
| `mode` | 总是 | `dry-run` 或 `execute`。 |
| `scanned_keys` | 总是 | SCAN 见到的 key 数。 |
| `would_delete` | dry-run | 预览将删除的记录数。 |
| `matched` | execute | 扫描命中、准备删除的记录数。 |
| `deleted` | execute | 实际删除成功数。 |
| `changed_or_missing` | execute | 因 value 变化或不存在而未删的数量。 |
| `records` | 总是 | dry-run 为完整记录明细；execute 为每条 key 的删除结果。 |

execute 时 `records[].status`：

| status | 含义 |
| --- | --- |
| `deleted` | value 未变，已删除，并从 pin zset 移除。 |
| `changed_or_missing` | key 不存在或 value 已变，未删除。 |

删除后再跑一次 `list-redis-records --count-only`，确认 `matched` 为 `0`。

---

## 5. 删除 YRFS 数据文件

命令：`delete-files`

按清单删除 YRFS 上的业务数据文件。`--execute` 时强制要求 Redis 已无引用。

### 5.1 配置示例

```yaml
shared_storage_cache_config:
  mount_path: "/mnt/real-yrfs"
  data_sub_path: "/yrcache_01"
  g35_cluster_id: "wbz-test-cluster"
  g35_osd_ids: [101, 102, 103, 104]

  redis_host: "127.0.0.1"
  redis_port: 6379
```

需要 `mount_path` 等 G35 字段；`--execute` 且带 `--require-no-redis-references` 时还需要能连 Redis。

### 5.2 执行方式

预演：

```bash
python g35_admin.py delete-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --dry-run \
  --output "$WORK_DIR/file-delete-dry-run.json"
```

实际删除：

```bash
python g35_admin.py delete-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --require-no-redis-references \
  --execute \
  --output "$WORK_DIR/file-delete-result.json"
```

参数说明：

| 参数 | 必填 | 作用 / 不给会怎样 |
| --- | --- | --- |
| `--config` | 是 | YAML 配置。 |
| `--manifest` | 是 | 受影响文件清单；路径必须相对 `mount_path`，禁止绝对路径和 `../`。 |
| `--dry-run` | 否 | 只预览；不指定 `--execute` 时默认 dry-run。dry-run 可不查 Redis。 |
| `--execute` | 否 | 实际删除。必须同时给 `--require-no-redis-references`，否则报错。 |
| `--require-no-redis-references` | `--execute` 时必填 | 先扫 Redis；若仍有引用则拒绝删除。dry-run 可不给。 |
| `--scan-batch` | 否 | 检查 Redis 时的 `SCAN COUNT`，默认 `1000`。 |
| `--redis-password` | 否 | Redis 密码。需要查 Redis 时才用到。 |
| `--output` | 否 | 可选写入 JSON 操作记录；**不给则打印到终端**。 |

安全约束：

- 先删 Redis，再删文件。
- 只删普通文件。
- 路径必须落在 `mount_path` 下。

### 5.3 产出报告

```json
{
  "mode": "dry-run",
  "matched": 2,
  "deleted": 0,
  "missing": 0,
  "files": [
    {
      "file": "yrcache_01/worker_id_1/cache_a_osds_306-307.dat",
      "status": "would_delete"
    },
    {
      "file": "yrcache_01/worker_id_2/cache_b_osds_305-306.dat",
      "status": "would_delete"
    }
  ]
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `mode` | `dry-run` 或 `execute`。 |
| `matched` | 清单中的文件条目数。 |
| `deleted` | execute 时实际删除数；dry-run 为 `0`。 |
| `missing` | 本来就不存在的数量。 |
| `files` | 每个文件的处理结果。 |

`files[].status`：

| status | 含义 |
| --- | --- |
| `would_delete` | dry-run：文件存在，执行时会删。 |
| `deleted` | execute：已删除。 |
| `missing` | 文件本来就不存在。 |

---

## 6. 创建探活文件

命令：`create-probes`

为 OSD 创建探活文件：先用 `yrcli --create ... --owners=<osd_id>` 建布局，再写入 `_g35_probe_payload`，最后校验布局与内容。

探活路径：

```text
{mount_path}/.yrcache_g35_probes/{g35_cluster_id}/probe_osd_{osd_id}.dat
```

### 6.1 配置示例

```yaml
shared_storage_cache_config:
  mount_path: "/mnt/real-yrfs"
  data_sub_path: "/yrcache_01"
  g35_cluster_id: "wbz-test-cluster"
  g35_osd_ids: [101, 102, 103, 104]
```

需要 G35 字段；`--execute` 还需要 `yrcli` 与 `yrcache.c_ops`。`--dry-run` 不需要 `c_ops`。

### 6.2 执行方式

预览：

```bash
python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --dry-run
```

创建全部 OSD：

```bash
python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --execute \
  --verbose \
  --output "$WORK_DIR/create-probes-result.json"
```

覆盖已有文件：

```bash
python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --execute \
  --overwrite \
  --verbose
```

只处理指定 OSD（一个或多个）：

```bash
python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --osd-id 101 103 \
  --execute --overwrite --verbose
```

参数说明：

| 参数 | 必填 | 作用 / 不给会怎样 |
| --- | --- | --- |
| `--config` | 是 | YAML 配置。 |
| `--osd-id` / `--osd_id` | 否 | 后跟一个或多个 ID。不给则处理 `g35_osd_ids` 全部。ID 必须在配置中且不能重复。 |
| `--overwrite` | 否 | 已存在时先 `unlink` 再 `yrcli --create` 重建。不给则跳过已存在文件。 |
| `--verbose` | 否 | 分步日志打到 stderr。不给则只有 JSON 结果。 |
| `--dry-run` | 否 | 只预览；不指定 `--execute` 时默认 dry-run。 |
| `--execute` | 否 | 实际创建并校验。需要 `yrcli` 与 `c_ops`。 |
| `--output` | 否 | 可选写入 JSON 操作记录；**不给则打印到终端**。 |

`--overwrite` 是“删旧再建”，不是只覆盖内容；布局由 `yrcli --create --stripesize=1m --stripecount=1 --pool=default --owners=<osd_id>` 决定。

### 6.3 产出报告

```json
{
  "mode": "execute",
  "cluster_id": "wbz-test-cluster",
  "mount_path": "/mnt/real-yrfs",
  "matched": 4,
  "created": 4,
  "skipped": 0,
  "probes": [
    {
      "osd_id": 101,
      "path": "/mnt/real-yrfs/.yrcache_g35_probes/wbz-test-cluster/probe_osd_101.dat",
      "bytes": 64,
      "verify": "ok",
      "status": "created"
    }
  ]
}
```

顶层字段：

| 字段 | 含义 |
| --- | --- |
| `mode` | `dry-run` 或 `execute`。 |
| `cluster_id` | `g35_cluster_id`。 |
| `mount_path` | 挂载点。 |
| `matched` | 本次处理的 OSD 数。 |
| `created` | execute 下尝试创建/覆盖的数量（含失败项）；dry-run 为 `0`。 |
| `skipped` | 因已存在且未 `--overwrite` 而跳过的数量。 |
| `probes` | 每个 OSD 的明细。 |

`probes[].status`（dry-run）：

| status | 含义 |
| --- | --- |
| `would_create` | 文件不存在，执行时会创建。 |
| `would_overwrite` | 文件已存在且指定了 `--overwrite`，执行时会覆盖。 |
| `would_skip_exists` | 文件已存在且未指定 `--overwrite`，执行时会跳过。 |

`probes[].status`（execute）：

| status | 含义 |
| --- | --- |
| `created` | 写入成功，且布局/内容校验通过（`verify=ok`）。 |
| `skipped_exists` | 已存在且未 `--overwrite`，跳过。 |
| `yrcli_error` | `yrcli --create` 失败；带 `error`。 |
| `created_but_layout_error` | 已写入，但查布局失败；带 `error_code`。 |
| `created_but_wrong_layout` | 已写入，但实际 OSD 不是 `[目标osd]`；带 `actual_osds`。 |
| `created_but_read_error` | 已写入，但回读失败；带 `error`。 |
| `created_but_content_error` | 已写入，但内容与 payload 不一致。 |

常见附加字段：

| 字段 | 含义 |
| --- | --- |
| `bytes` | 写入字节数。 |
| `verify` | 校验子状态：`ok` / `layout_error` / `wrong_layout` / `read_error` / `content_error`。 |
| `error` | 失败原因。 |
| `error_code` | 布局查询错误码。 |
| `actual_osds` | 实际布局 OSD 列表。 |

期望：全部 `status=created` 且 `verify=ok`。`created_but_wrong_layout` 不建议忽略。

---

## 7. 验证恢复

命令：`verify-recovery`

验证 `--manifest` 中受影响文件与 Redis 记录是否已清理完成。

只检查两项：

1. **YRFS**：清单中的文件是否已全部不存在。
2. **Redis**：是否仍有记录指向清单中的文件。

不检查探活文件、运行时状态或其他 OSD。检查范围完全由 `--manifest` 决定。

### 7.1 配置示例

与第 3 节相同，需要 G35 字段 + Redis 连接。不需要 `yrcache.c_ops`。

### 7.2 执行方式

```bash
python g35_admin.py verify-recovery \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --output "$WORK_DIR/verify-recovery-result.json"
```

参数说明：

| 参数 | 必填 | 作用 / 不给会怎样 |
| --- | --- | --- |
| `--config` | 是 | YAML 配置。 |
| `--manifest` | 是 | 第 1 节生成的受影响文件清单（`--report` 产物）。 |
| `--scan-batch` | 否 | Redis `SCAN COUNT`，默认 `1000`。 |
| `--redis-password` | 否 | Redis 密码。 |
| `--output` | 否 | 可选写入 JSON 操作记录；**不给则打印到终端**。 |

### 7.3 产出报告

清理完成（`ok=true`）：

```json
{
  "ok": true,
  "mount_path": "/mnt/real-yrfs",
  "manifest_files": 2,
  "remaining_files": [],
  "remaining_redis_records": []
}
```

仍有残留（`ok=false`）：

```json
{
  "ok": false,
  "mount_path": "/mnt/real-yrfs",
  "manifest_files": 2,
  "remaining_files": [
    "yrcache_01/worker_id_1/cache_a_osds_306-307.dat"
  ],
  "remaining_redis_records": [
    {
      "key": "1:2:3:abc:def",
      "file_path": "yrcache_01/worker_id_1/cache_a_osds_306-307.dat",
      "offset": 0,
      "real_size": 4096,
      "alloc_size": 4096
    }
  ]
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `ok` | 清单文件与 Redis 记录是否均已清理完成。 |
| `mount_path` | YRFS 挂载点。 |
| `manifest_files` | 清单中的文件总数。 |
| `remaining_files` | 仍存在于 YRFS 上的清单文件。期望 `[]`。 |
| `remaining_redis_records` | 仍引用清单文件的 Redis 记录。期望 `[]`。 |

退出码：`ok=true` 返回 `0`，否则返回 `2`（参数/依赖错误仍为 `1`）。

---

## 推荐完整流程

```bash
export YRCACHE_CONFIG=/etc/yrcache/yrcache.yaml
export FAILED_OSD=306
export WORK_DIR=/var/tmp/yrcache-g35-osd-${FAILED_OSD}
mkdir -p "$WORK_DIR"

# 1. 生成清单
python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --osd-id "$FAILED_OSD" \
  --report "$WORK_DIR/affected-files.json"

# 2. 检查清单规模
python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --summary

# 3. 查询 Redis（也可用 delete-redis-records --dry-run 做预览）
python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --count-only

# 4. 删除 Redis
python g35_admin.py delete-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --dry-run

python g35_admin.py delete-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --execute \
  --output "$WORK_DIR/redis-delete-result.json"

python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --count-only

# 5. 删除文件
python g35_admin.py delete-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --dry-run

python g35_admin.py delete-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --require-no-redis-references \
  --execute \
  --output "$WORK_DIR/file-delete-result.json"

# 6. 创建探活
python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --execute \
  --overwrite \
  --verbose \
  --output "$WORK_DIR/create-probes-result.json"

# 7. 验证清理完成
python g35_admin.py verify-recovery \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --output "$WORK_DIR/verify-recovery-result.json"
```

## 常见问题

### 为什么 `yrcache-g35-admin create-probes` 报 invalid choice？

说明执行的是环境中旧版命令，不是当前目录的 `g35_admin.py`。请用：

```bash
python g35_admin.py create-probes --config yrcache.yaml --dry-run
```

### 为什么 dry-run 能跑，execute 报 `需要已编译的 yrcache.c_ops`？

`dry-run` 只预览路径。`execute` 需要生成 payload 并校验 YRFS 布局，必须能 `import yrcache.c_ops`。

### `created_but_wrong_layout` 是否可以忽略？

不建议。表示内容已写，但文件没有只落在目标 OSD，后续探活会判该 OSD 不合格。

### `delete-files --execute` 为什么强制要求 `--require-no-redis-references`？

防止 Redis 仍命中旧文件。只有 Redis 无引用后才允许删 YRFS 数据文件。

### manifest 里能不能写绝对路径？

不能。必须是相对 `mount_path` 的路径；绝对路径和越过挂载点的路径都会被拒绝。

### 不指定 `--output` 会生成文件吗？

不会（`list-files --osd-id` 除外，它必须指定 `--report`）。可选的 `--output` 不给时，结果只打印到终端 stdout。

### `--report` 和 `--output` 有什么区别？

- `--report`：仅用于 `list-files --osd-id`，**必填**，生成供下游 `--manifest` 使用的清单文件。
- `--output`：其他所有命令的可选参数，只用于留档或人工查看，不会被其他命令读取。

### `list-redis-records` 和 `delete-redis-records --dry-run` 有何区别？

底层扫描相同，命中集合一致。前者偏只读查询（支持 `--count-only`），后者是删除预演并可继续 `--execute`。预览阶段二选一即可。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功。 |
| `1` | 参数、配置、依赖、Redis、文件或其他执行错误。 |
| `2` | `verify-recovery` 清理未完成（`remaining_files` 或 `remaining_redis_records` 非空）。 |

## 安全建议

- 生产环境先 `--dry-run`，核对数量后再 `--execute`。
- 删 Redis 后必须再查一次，确认 `matched=0`。
- 不要跳过 `--require-no-redis-references`。
- 探活创建后检查全部 `status=created` 且 `verify=ok`。
