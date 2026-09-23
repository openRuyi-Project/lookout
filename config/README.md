# 监控配置

| 修改内容 | 唯一生效入口 |
|---|---|
| 上游身份、发布筛选、版本线、版本前缀 | [`versions/nvchecker.toml`](versions/nvchecker.toml) 中对应的原生表 |
| compare/watch/comparable 等包策略、monitor 身份 | [`tracker.toml`](tracker.toml) 中的 `[packages.<name>]` |
| BuildSystem 配色 | `tracker.toml` 的 `[openruyi.buildsystems]` |
| 周期、代理、凭据和路径 | 部署的外部配置及环境；凭据不入库 |

默认只加载 `collector.nvchecker_config` 指向的原生文件。旁边的单包 TOML
不是覆盖入口；配置加载不根据包名或 Source0 自动生成规则。

## 修改与验证

例如在 `versions/nvchecker.toml` 添加或修改：

```toml
[python-requests]
source = "pypi"
pypi = "requests"
```

普通包名与 track 同名，无需额外绑定。需要旁路观察时，在同一个原生文件
定义 `widget@prerelease` 表，再在 tracker 中关联：

```toml
[packages.widget]
watch = ["widget@prerelease"]
```

正式 release 仍是主比较；预发布只作为显式 watch。保留维护线筛选与
provider 排序语义，不因某个 tag 存在就把它当作正式 release。KDE/Qt 等
共享主页的组件，必须分别核对组件身份和发布系列。

从仓库根目录运行：

```sh
PYTHONPATH=backend python -m tracker.package explain python-requests \
  --config config/tracker.toml --format human
PYTHONPATH=backend python -m tracker.package check python-requests \
  --config config/tracker.toml --format human
```

`explain` 定位实际生效的原生表与包策略；`check` 调用 nvchecker，不写生产
快照。使用 `--output FILE` 保存完整报告；JSON 输出保留机器接口。

## 新增软件包

OBS 新包自动进入库存；没有版本规则时保留 Untracked，其他信息仍可展示。
有证据的规则候选由 setup/discover 离线生成，不参与运行时规则合成：

```sh
PYTHONPATH=backend python -m tracker.package setup 包名 \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3 \
  --output /tmp/new-package-proposal.json
```

审阅候选的 Source0、SPEC 身份和发布策略后，加入原生文件，再运行 check。
`entry=null` 是证据不足，不是成功。普通上游别名是独立身份信息；SPEC Name
与目录不一致则是打包问题，应保留 `TODO(drop)`，等上游修正并验证后删除。

## 推广到运行配置

保留修改前配置为 `BASE`、修改副本为 `CANDIDATE`、运营配置为 `RUNTIME`。
用同一份只读快照生成审阅目录，再生成尚不存在的新配置目录：

```sh
PYTHONPATH=backend python -m tracker.package plan \
  --base-config "$BASE/tracker.toml" --config "$CANDIDATE/tracker.toml" \
  --runtime-config "$RUNTIME/tracker.toml" --db "$SNAPSHOT_COPY" \
  --output "$REVIEW" --format human
PYTHONPATH=backend python -m tracker.package apply \
  --review "$REVIEW" --runtime-config "$RUNTIME/tracker.toml" \
  --destination "$PREPARED" --format human
```

先审阅 REVIEW 的有效规则、包策略和文件差异。运营冲突应停止，不能用仓库
默认配置覆盖本地凭据或设置。apply 不切换线上挂载；升级、预检与回滚统一见
[部署说明](../docs/deployment.md)。
