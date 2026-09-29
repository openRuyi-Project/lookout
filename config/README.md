# 监控配置

## 找到修改入口

| 修改内容 | 文件与位置 |
|---|---|
| 上游版本身份、发布筛选、版本线、前缀 | [`versions/nvchecker.toml`](versions/nvchecker.toml)，按 track 名查找 |
| 包的 compare/watch/comparable 策略、monitor 身份例外 | [`packages.toml`](packages.toml)，按包名查找 |
| 目标、周期、路径、启用的 monitors | [`tracker.toml`](tracker.toml) |
| BuildSystem 配色、图标 | `tracker.toml` 的 `[openruyi.buildsystems]`；`icon` 引用 `frontend/src/assets/logos/catalog.json` 的本地资产 |
| 依赖身份到 openRuyi 源包的映射 | `tracker.toml` 的 `[openruyi.dependencies]` |

`collector.nvchecker_config` 和 `packages_config` 是显式文件引用，相对于
`tracker.toml` 所在目录解析。同目录其他 TOML 不参与加载。运营配置放在源码
目录外；凭据不入库。修改抓取或展示代码，见[贡献指南](../CONTRIBUTING.md#where-to-edit)。

先用 CLI 定位实际生效的表、行号和实现：

```sh
PYTHONPATH=backend python -m tracker.package explain python-requests \
  --config config/tracker.toml --format human
PYTHONPATH=backend python -m tracker.monitors explain openssl --monitor eol \
  --config config/tracker.toml --format human
```

`explain` 不连接 provider。monitor 命令增加 `--db /path/to/snapshot-copy.sqlite3`
可同时查看已采集的 Source0 和检查状态；不提供快照时只解释配置。

## 修改版本规则

`nvchecker.toml` 使用原生 nvchecker 格式。短规则放在文件根部、第一个表头之前：

```toml
python-requests = { source = "pypi", pypi = "requests" }
```

超过 120 字符或需要字段说明时使用多行表；同一 track 只能定义一次。
正则可用 TOML `'字面量字符串'` 避免双重转义。

包名默认对应同名 track。主比较追踪正式 release；维护线和组件身份必须明确，
不能因为 KDE/Qt 等组件共享主页就共享版本。预发布仅作显式旁路观察：

```toml
# nvchecker.toml：另定义旁路 track
["widget@prerelease"]
source = "pypi"
pypi = "widget"
use_pre_release = true
```

```toml
# packages.toml：关联旁路，主比较仍使用 widget
[widget]
watch = ["widget@prerelease"]
```

修改后运行独立检查；它调用 nvchecker，但不写生产快照：

```sh
PYTHONPATH=backend python -m tracker.package check python-requests \
  --config config/tracker.toml --format human
```

`--output FILE` 保存完整报告；省略 `--format human` 时输出 JSON。

### 项目提供的 nvchecker sources

插件位于 [`backend/nvchecker_source/`](../backend/nvchecker_source/)，随 backend 安装。
它们使用 nvchecker 的请求、缓存和版本处理机制：

| source | 配置身份 | 返回的候选 |
|---|---|---|
| `crates_index` | `cratesio = "accesskit"` | Cargo sparse index 中未 yanked 的版本 |
| `go_proxy` | `url = "…/@latest"` | 正式 Go release，排除预发布和 pseudo-version |
| `anitya_stable` | `anitya_id = 7306` | Anitya `stable_versions` 的第一项，保留 provider 排序 |

`crates_index`、`anitya_stable` 可用自定义 `url` 替代身份字段，但不能两者同时
填写。前缀和维护线筛选仍写在规则中。Anitya 若需**先筛维护线再取首项**，使用
原生 `jq` 的 `first(... | select(...))`，不能在 `anitya_stable` 取首项后补筛。

工作区直接运行时用 `PYTHONPATH=backend nvchecker -c config/versions/nvchecker.toml`；
安装 backend 后无需设置该路径。含插件的配置须搭配提供插件的镜像。

### Git 快照

只有 Source0 的完整 commit 与 RPM `+git日期.短hash` 一致，才能用分支头比较。
目前识别 GitHub/codeload、GitLab、Forgejo 和 cgit 归档。分支从仓库引用确认，
不能只凭包名或 `+git` 推断；本地 tarball、短 Source0 SHA、混合 commit、独立
子组件需额外证据。

```toml
# nvchecker.toml
["widget@commits"]
source = "git"
git = "https://github.com/example/widget"
use_commit = true
branch = "main"
```

```toml
# packages.toml：保留原 release 规则作为旁路
[widget]
compare = "widget@commits"
watch = ["widget"]
```

相同 hash 为 current，不同为 changed，不推断提交先后或正式版本升级。
主页面缩写为 `YYYYMMDD.xxxxxx`；当前日期取 RPM 快照版本，目标日期只取
`revision_creation_time`（UTC）。原生 `git` 不返回提交时间，目标仅显示短 hash；
需要时间时可选 GitHub/GitLab/Forgejo 的分支 API，并承担其配额。短 hash 冲突时
延长，API 和详情保留完整值。commit 不作为 License/Requires 的升级版本输入。

## 修改其他 monitor 的身份

支持 registry 身份推导的 monitor 优先使用已观测 Source0，缺失时使用版本规则
身份。EOL 需要显式配置产品和周期。仅在需要身份例外或额外字段时添加包策略，例如：

```toml
# packages.toml
[openssl.monitors.eol]
product = "openssl"
cycle_parts = 2

[widget.monitors.license]
pypi = "upstream-widget"
```

用 `tracker.monitors explain` 检查实际输入；缺少 License 表不等于未覆盖。
声明的依赖找不到源包映射时保留未知，不按包名猜测。各 adapter 的字段和检查
入口见[monitor 接入指南](../docs/monitor-porting.md)。

| 上游身份 | 自动复用的观察 |
|---|---|
| PyPI | Security、LicenseDiff、RuntimeDeps、Yanked |
| crates.io | Security、LicenseDiff、Yanked；工具链要求不作为运行依赖 |
| Go module | Security、deps.dev 许可证、Go proxy 撤回声明；模块构建图不作为运行依赖 |
| CPAN distribution | 许可证、静态 runtime prerequisites；module 版本不等同于 distribution 版本 |
| Source0 完整 commit | OSV commit 查询；仓库命中不等于子包适用 |

运行依赖映射在 `[openruyi.dependencies]`；PEP 508 的目标平台在
`[openruyi.dependency_environments.pep508]`。Python 版本取映射源包的有效观察，
不取采集机版本。未配置的架构或可选 feature 条件保持未知。CPAN 的
`dynamic_config`、无可靠 SPDX 声明和不支持的 go.mod 语法不会被记为正常结果。

## 新增软件包

OBS 库存中的包即使没有版本规则也会显示，其他 monitor 独立工作。
`setup` 离线读取快照，目前仅从 crates.io Source0 提出规则候选。审阅身份和
发布策略后，再加入原生文件：

```sh
PYTHONPATH=backend python -m tracker.package setup 包名 \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3 \
  --output /tmp/new-package-proposal.json
```

`entry=null` 表示未生成候选，不等于没有上游；来源尚不支持或证据不足都可能
产生该结果，需查看 `reason`。上游别名属于身份映射；SPEC Name 与目录不一致
则是打包问题，例外需注明 `TODO(drop)` 的删除条件。

`discover` 支持更多来源，可查询 Anitya；不加 `--verify` 也可能联网。
`--verify` 额外调用 nvchecker 验证候选，失败候选不进入输出规则，整个过程
不改运行配置：

```sh
PYTHONPATH=backend python -m tracker.monitors.version.discover \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3 \
  --output /tmp/version-candidates --verify
```

Cargo 预发布 Source0 保留原始 release 身份；RPM 展开的 Version 用于显示，
registry 查询不能把 `1.2.0-rc.1` 改成 `1.2.0`。主比较仍遵循配置的正式发布线。

## 推广到运行配置

`plan/apply` 离线推广版本规则、包策略、BuildSystem 配色和 monitor 设置，
不需要快照。其余运营设置（包括依赖映射）须在新运营配置目录中单独修改并预检。
保留修改前目录 `BASE`、修改副本 `CANDIDATE` 和运营目录 `RUNTIME`；
`REVIEW` 和 `PREPARED` 必须是尚不存在的目录：

```sh
PYTHONPATH=backend python -m tracker.package plan \
  --base-config "$BASE/tracker.toml" --config "$CANDIDATE/tracker.toml" \
  --runtime-config "$RUNTIME/tracker.toml" \
  --output "$REVIEW" --format human
PYTHONPATH=backend python -m tracker.package apply \
  --review "$REVIEW" --runtime-config "$RUNTIME/tracker.toml" \
  --destination "$PREPARED" --format human
```

在 apply 前审阅 REVIEW 中的规则、包策略和文件差异。冲突或输入漂移会拒绝推广；
不能用仓库默认配置覆盖运营设置。apply 只生成新目录，不切换挂载；后续预检、
升级和回滚见[部署说明](../docs/deployment.md)。

## SPEC 解析输入

`[spec].extra_macro_packages` 指定受管理 Git 树中的额外宏包；
`[spec.local_sources]` 映射原生 `%include` 所需的包内文件，如 patch series。
这些输入记录哈希，在既有沙箱中解析，变化会使缓存失效，缺失则报错。
解析不下载 Source 压缩包，也不用文本猜测替代 RPM 展开的 Version。
