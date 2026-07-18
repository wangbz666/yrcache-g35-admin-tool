# YRCache G3.5 Admin Tool

`g35_admin.py` 是一个面向 YRCache G3.5 运维场景的单文件命令行工具。它用于处理 OSD 故障、更换、清理和恢复验证流程，包含受影响文件扫描、Redis 元数据查询与删除、YRFS 数据文件删除、探活文件创建，以及恢复状态校验。

本项目只管理一个脚本：

```text
g35_admin.py
```

## 适用场景

当 G3.5 的某个 OSD 故障或换盘后，需要清理所有落在该 OSD 上的旧缓存数据，避免 Redis 命中后读取到不存在或不可用的 YRFS 文件。本工具按安全顺序执行：

1. 扫描挂载点，生成受影响文件清单。
2. 查询 Redis 中指向这些文件的记录。
3. 先删除 Redis 记录。
4. 再删除 YRFS 数据文件。
5. 重建或创建探活文件。
6. 验证文件、Redis、探活文件和运行时检查状态。

重要原则：必须先删 Redis 记录，再删数据文件。反向操作会产生“Redis 命中但文件已不存在”的窗口。

## 运行方式

当前项目没有打包入口，直接用 Python 执行脚本：

```bash
python g35_admin.py --help
python g35_admin.py <command> [options]
```

如果你已经把它安装成 console script，也可以用 `yrcache-g35-admin`，但本项目默认以 `python g35_admin.py` 为准。

建议使用 Python 3.8+。脚本使用了类型标注语法和 `from __future__ import annotations`，过旧 Python 版本可能无法运行。

## 依赖

基础功能需要：

```bash
pip install redis PyYAML
```

探活文件校验和创建还需要运行环境中存在已编译的 `yrcache.c_ops`，并暴露以下能力：

- `_g35_query_yrfs_file_osds(path)`：查询 YRFS 文件实际分布在哪些 OSD 上。
- `_g35_probe_payload(cluster_id, osd_id)`：生成探活文件内容。

`create-probes --dry-run` 不加载 `c_ops`，只预览路径；`--execute` 会调用 `yrcli --create` 建立 YRFS 布局，加载 `c_ops` 写入内容并校验。

## 配置文件

工具读取 YAML 中的 `shared_storage_cache_config`：

```yaml
shared_storage_cache_config:
  mount_path: "/mnt/wbz"
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
| `mount_path` | 是 | YRFS 挂载点。所有文件扫描、删除、探活路径都基于它。 |
| `g35_cluster_id` | 是 | G3.5 集群 ID，用于探活路径和探活内容。 |
| `g35_osd_ids` | 是 | 当前集群有效 OSD ID 列表，必须是非重复无符号 32 位整数。 |
| `redis_host` | Redis 命令必填 | Redis 地址。也可通过 `--redis-host` 覆盖。 |
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

所有会改变数据的命令都支持 dry-run / execute 模式：

- `--dry-run`：只预览，默认安全行为。
- `--execute`：实际执行。

## 1. 生成受影响文件清单

```bash
export YRCACHE_CONFIG=/etc/yrcache/yrcache.yaml
export FAILED_OSD=7
export WORK_DIR=/var/tmp/yrcache-g35-osd-${FAILED_OSD}
mkdir -p "$WORK_DIR"

python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --osd-id "$FAILED_OSD" \
  --output "$WORK_DIR/affected-files.json"
```

扫描逻辑：

- 递归扫描 `mount_path` 下所有普通文件。
- 精确解析文件名中最后一个 `_osds_` 后面的 OSD ID 列表。
- OSD ID 列表格式为十进制整数，多个 OSD 用 `-` 分隔，例如 `_osds_101-102-103.dat`。
- 不使用 `*7*` 这类模糊匹配，所以不会把 `17`、`70`、`107` 误判成 `7`。

输出示例：

```json
{
  "mount_path": "/mnt/wbz",
  "osd_id": 7,
  "files": [
    "worker_id_1/cache_a_osds_7-8.dat",
    "worker_id_2/cache_b_osds_3-7.dat"
  ]
}
```

## 2. 检查受影响文件清单

```bash
python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --summary
```

当前 `--summary` 表示按清单做摘要检查，输出包括：

```json
{
  "mount_path": "/mnt/wbz",
  "files": 2,
  "existing": 2
}
```

字段说明：

- `files`：清单中的文件数量。
- `existing`：当前仍存在于 `mount_path` 下的文件数量。

清单可以是 JSON 数组：

```json
[
  "worker_id_1/cache_a_osds_7-8.dat"
]
```

也可以是包含 `files` 字段的对象，也就是 `list-files --osd-id` 的原始输出格式。

## 3. 查询 Redis 记录

```bash
python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --scan-batch 1000 \
  --output "$WORK_DIR/affected-redis-records.json"
```

实现细节：

- 使用 Redis `SCAN` 分批遍历，不使用 `KEYS *`。
- 只处理看起来像 YRCache 记录的 string key。
- 反序列化 string value 中的 SharedValueMeta。
- 只保留 `location.file_path` 指向清单中文件的记录。

输出示例：

```json
{
  "scanned_keys": 12876,
  "matched": 2,
  "records": [
    {
      "key": "1:2:3:abc:def",
      "file_path": "worker_id_1/cache_a_osds_7-8.dat",
      "offset": 0,
      "real_size": 4096,
      "alloc_size": 4096
    }
  ]
}
```

只看数量：

```bash
python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --count-only
```

## 4. 删除 Redis 记录

先预演：

```bash
python g35_admin.py delete-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --dry-run \
  --report "$WORK_DIR/redis-delete-dry-run.json"
```

确认预演数量和查询结果一致后执行：

```bash
python g35_admin.py delete-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --execute \
  --report "$WORK_DIR/redis-delete-report.json"
```

删除逻辑使用 Lua 条件删除：

- 当前 key 的 value 和扫描时记录的 value 完全一致才删除。
- 同时从 `redis_pin_key` 指定的 zset 中 `ZREM`。
- 如果 value 已变或 key 已不存在，则不删除，并标记为 `changed_or_missing`。

Redis 删除状态：

| status | 含义 |
| --- | --- |
| `deleted` | key 的 value 未变化，已成功删除，并从 pin zset 移除。 |
| `changed_or_missing` | key 不存在或 value 已变化。工具不会强删，避免误删新写入记录。 |

再次确认 Redis 引用为 0：

```bash
python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --scan-batch 1000 \
  --count-only
```

## 5. 删除 YRFS 数据文件

先预演：

```bash
python g35_admin.py delete-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --dry-run \
  --report "$WORK_DIR/file-delete-dry-run.json"
```

确认 Redis 查询结果为 0 后执行：

```bash
python g35_admin.py delete-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --require-no-redis-references \
  --execute \
  --report "$WORK_DIR/file-delete-report.json"
```

安全约束：

- `--execute` 时必须指定 `--require-no-redis-references`。
- 如果仍有 Redis 记录引用清单中文件，命令拒绝删除。
- manifest 中只能包含相对路径。
- 禁止路径越过 `mount_path`，例如 `../xxx` 会被拒绝。
- 只删除普通文件。

文件删除状态：

| status | 含义 |
| --- | --- |
| `would_delete` | dry-run 中发现文件存在，执行时会删除。 |
| `deleted` | execute 中文件已删除。 |
| `missing` | 文件本来就不存在。 |

## 6. 创建探活文件

为配置中的每个 OSD 创建一个探活文件：

```bash
python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --dry-run
```

实际创建：

```bash
python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --execute \
  --verbose \
  --report "$WORK_DIR/create-probes-report.json"
```

覆盖已有探活文件：

```bash
python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --execute \
  --overwrite \
  --verbose \
  --report "$WORK_DIR/create-probes-report.json"
```

`--verbose` 会把每个 OSD 的分步日志打到 stderr（生成 payload → `yrcli --create` → 写入内容 → 校验），失败时会带上 `yrcli` 的 stdout/stderr，便于定位后手动复现。

路径格式：

```text
{mount_path}/.yrcache_g35_probes/{g35_cluster_id}/probe_osd_{osd_id}.dat
```

配置示例：

```yaml
shared_storage_cache_config:
  mount_path: "/mnt/wbz"
  g35_cluster_id: "wbz-test-cluster"
  g35_osd_ids: [101, 102, 103, 104]
```

执行后会创建：

```text
/mnt/wbz/.yrcache_g35_probes/wbz-test-cluster/probe_osd_101.dat
/mnt/wbz/.yrcache_g35_probes/wbz-test-cluster/probe_osd_102.dat
/mnt/wbz/.yrcache_g35_probes/wbz-test-cluster/probe_osd_103.dat
/mnt/wbz/.yrcache_g35_probes/wbz-test-cluster/probe_osd_104.dat
```

内容说明：

- 文件不是空文件。
- 内容由 `_g35_probe_payload(g35_cluster_id, osd_id)` 生成。
- 该 payload 和 `verify_probe` 后续校验使用的是同一套逻辑。
- 创建时先调用 `yrcli --create --stripesize=1m --stripecount=1 --pool=default <path> --owners=<osd_id>` 建立 YRFS 布局，再写入 payload。
- 覆盖已有探活文件时会先删除旧文件，再重新执行 `yrcli --create`。
- 写完后立即调用 `verify_probe`，校验文件布局和内容。

注意：运行环境中需要能直接执行 `yrcli`。如果 `yrcli` 创建失败，报告会显示 `yrcli_error`；如果布局未落在唯一目标 OSD，报告会显示 `created_but_wrong_layout`。

### create-probes 状态说明

dry-run 状态：

| status | 含义 |
| --- | --- |
| `would_create` | 目标探活文件不存在，执行时会创建。 |
| `would_overwrite` | 目标探活文件已存在，且指定了 `--overwrite`，执行时会覆盖。 |
| `would_skip_exists` | 目标探活文件已存在，未指定 `--overwrite`，执行时会跳过。 |

execute 状态：

| status | 含义 |
| --- | --- |
| `created` | 已写入探活文件，并且布局和内容校验通过。 |
| `skipped_exists` | 文件已存在，未指定 `--overwrite`，跳过未改。 |
| `yrcli_error` | `yrcli --create` 执行失败。报告中会带 `error`。 |
| `created_but_layout_error` | 文件已写入，但查询 YRFS 布局失败。报告中会带 `error_code`。 |
| `created_but_wrong_layout` | 文件已写入，但实际布局不是只落在目标 OSD。报告中会带 `actual_osds`。 |
| `created_but_read_error` | 文件已写入，但回读文件失败。报告中会带 `error`。 |
| `created_but_content_error` | 文件已写入，但内容与 `_g35_probe_payload` 期望值不一致。 |

成功报告示例：

```json
{
  "mode": "execute",
  "cluster_id": "wbz-test-cluster",
  "mount_path": "/mnt/wbz",
  "matched": 4,
  "created": 4,
  "skipped": 0,
  "probes": [
    {
      "osd_id": 101,
      "path": "/mnt/wbz/.yrcache_g35_probes/wbz-test-cluster/probe_osd_101.dat",
      "bytes": 64,
      "verify": "ok",
      "status": "created"
    }
  ]
}
```

## 7. 验证恢复

```bash
python g35_admin.py verify-recovery \
  --config "$YRCACHE_CONFIG" \
  --osd-id "$FAILED_OSD" \
  --manifest "$WORK_DIR/affected-files.json" \
  --ack-runtime-checks \
  --report "$WORK_DIR/verify-recovery-report.json"
```

静态检查包括：

- manifest 中的受影响文件全部不存在。
- Redis 中不存在指向这些文件的记录。
- 配置中的所有 OSD 探活文件都存在、布局正确、内容校验通过。

运行时检查需要人工确认：

- 各 YRCache 实例已经移除目标 OSD 的异常标记。
- 文件访问模式进入 `CLOSED`，或因其他异常 OSD 保持符合预期的 `DEGRADED`。
- 原受影响 KV Cache 查询返回缓存未命中，并可重新计算。

未指定 `--ack-runtime-checks` 时，即使静态检查全部通过，命令也会返回未完成状态，退出码为 `2`。

### verify_probe 状态说明

`verify-recovery` 的 `probes` 字段来自单个探活文件校验：

| status | 含义 |
| --- | --- |
| `ok` | 文件布局只包含目标 OSD，且内容完全符合预期。 |
| `layout_error` | 查询 YRFS 布局失败。报告中会带 `error_code`。 |
| `wrong_layout` | 查询成功，但实际 OSD 列表不是 `[osd_id]`。报告中会带 `actual_osds`。 |
| `read_error` | 文件布局正常，但读取文件内容失败。报告中会带 `error`。 |
| `content_error` | 文件可读，但内容与 `_g35_probe_payload` 生成值不一致。 |

## 推荐完整流程

```bash
export YRCACHE_CONFIG=/etc/yrcache/yrcache.yaml
export FAILED_OSD=7
export WORK_DIR=/var/tmp/yrcache-g35-osd-${FAILED_OSD}
mkdir -p "$WORK_DIR"

python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --osd-id "$FAILED_OSD" \
  --output "$WORK_DIR/affected-files.json"

python g35_admin.py list-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --summary

python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --scan-batch 1000 \
  --output "$WORK_DIR/affected-redis-records.json"

python g35_admin.py delete-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --dry-run

python g35_admin.py delete-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --execute \
  --report "$WORK_DIR/redis-delete-report.json"

python g35_admin.py list-redis-records \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --scan-batch 1000 \
  --count-only

python g35_admin.py delete-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --dry-run

python g35_admin.py delete-files \
  --config "$YRCACHE_CONFIG" \
  --manifest "$WORK_DIR/affected-files.json" \
  --require-no-redis-references \
  --execute \
  --report "$WORK_DIR/file-delete-report.json"

python g35_admin.py create-probes \
  --config "$YRCACHE_CONFIG" \
  --execute \
  --overwrite \
  --report "$WORK_DIR/create-probes-report.json"

python g35_admin.py verify-recovery \
  --config "$YRCACHE_CONFIG" \
  --osd-id "$FAILED_OSD" \
  --manifest "$WORK_DIR/affected-files.json" \
  --ack-runtime-checks \
  --report "$WORK_DIR/verify-recovery-report.json"
```

## 常见问题

### 为什么 `yrcache-g35-admin create-probes` 报 invalid choice？

说明你执行的是环境中已安装的旧版命令，不是当前目录的 `g35_admin.py`。直接运行：

```bash
python g35_admin.py create-probes --config yrcache.yaml --dry-run
```

也可以检查当前命令指向哪里：

```bash
which yrcache-g35-admin
python -c "import yrcache.tools.g35_admin as m; print(m.__file__)"
```

### 为什么 dry-run 能跑，execute 报 `需要已编译的 yrcache.c_ops`？

`dry-run` 只预览路径，不需要底层 YRFS 能力。`execute` 需要生成探活 payload，并在写入后校验 YRFS 布局，所以必须在包含已编译 `yrcache.c_ops` 的运行环境中执行。

### `created_but_wrong_layout` 是否可以忽略？

不建议忽略。它表示文件内容已写入，但 YRFS 报告该探活文件没有只分布在目标 OSD 上。后续探活判断会认为该 OSD 的探活文件不合格。

### `delete-files --execute` 为什么强制要求 `--require-no-redis-references`？

这是防止 Redis 仍然命中旧文件。只有 Redis 中没有引用后，才允许删除 YRFS 数据文件。

### manifest 里能不能写绝对路径？

不能。manifest 必须是相对 `mount_path` 的路径。绝对路径和越过挂载点的路径都会被拒绝。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功。 |
| `1` | 参数、配置、依赖、Redis、文件或其他执行错误。 |
| `2` | `verify-recovery` 静态检查可能通过，但未完成所有运行时确认，或整体恢复状态未达到 `ok`。 |

## 安全建议

- 生产环境先跑 `--dry-run`，保存报告并人工核对数量。
- 删除 Redis 后必须重新查询确认 `matched` 为 `0`。
- 不要跳过 `--require-no-redis-references`。
- 探活文件创建后检查 `status` 是否全部为 `created`，且 `verify` 是否全部为 `ok`。
- `verify-recovery --ack-runtime-checks` 只应在人工确认运行时条件后使用。
