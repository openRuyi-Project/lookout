# 监控配置

[`versions/nvchecker.toml`](versions/nvchecker.toml) 是原生 nvchecker 规则的编辑入口；同目录的 `包名.toml` 保存单包特例。每个表都是一个独立、可直接交给 nvchecker 的规则；不在运行时展开模板。规则策略在 tracker.toml 的 [packages.<name>] 中维护。

| 修改内容 | 位置 |
|---|---|
| 上游地址、正式发布筛选、版本线、前缀 | `versions/nvchecker.toml`：`group` 成员；特例写同目录 `包名.toml` |
| 显式预发布旁路、比较别名、特殊版本线标签 | `versions/nvchecker.toml`：`binding` |
| EOL/CVE 等 monitor 身份 | `tracker.toml`：`packages` |
| BuildSystem 配色 | `tracker.toml`：`openruyi.buildsystems` |
| 代理、密钥、定时周期 | 部署外部配置；凭据不入库 |

## 共享规则

同一类规则的公共字段写一次，每个包只添加一个成员。参数只做字符串替换，不承担版本推断或排序。

```toml
schema = 1

[group.pypi]
source = "pypi"
pypi = "{pypi}"

[group.pypi.packages]
python-requests = { pypi = "requests" }
python-flask = { pypi = "Flask" }
```

成员名也能直接代入；只有组件身份、发布线和筛选策略确实一致时才能共用一组：

```toml
[group.qt6]
package = "qt6-{name}"
packages = ["qtbase", "qtwayland"]
source = "git"
git = "https://code.qt.io/qt/{name}.git"
prefix = "v"
include_regex = '^v6\.[0-9]+\.[0-9]+$'
```

这两种成员写法二选一。集合之间重名仍报错。**同目录的 `包名.toml` 优先于集合，整条替换，不逐字段继承**；无需从集合移除包名。删除特例即可恢复集合规则。修改公共字段只影响未被特例覆盖的成员。

例如 `perl-Text-Tabs+Wrap.toml` 内容：

```toml
source = "cpan"
cpan = "Text-Tabs+Wrap"
```

正式 release 是主比较；预发布只有显式旁路才采集，不能用任意 tag 冒充 release。独立旁路 track 配好后，用 `[binding."包名"]` 的 `watch = ["包名@prerelease"]` 关联。普通同名比较无需 binding。

## 定位、检查与新增

```sh
PYTHONPATH=backend python -m tracker.package explain 包名 --config config/tracker.toml --format human
PYTHONPATH=backend python -m tracker.package check 包名 --config config/tracker.toml --format human
```

`explain` 返回实际生效文件、表、成员行及展开后的策略，并标明特例是否覆盖集合；无规则时也返回新增位置。`check` 执行真实 nvchecker，不写生产快照。公共策略变更须检查全部受影响成员，不只检查一个示例包。

新增已有家族的软件，添加一个成员；没有合适家族时添加一个 `包名.toml`。不需要改 Python，也不改缓存数据库。KDE/Qt 必须按组件与发布系列区分，不能共享主页就共享版本。

OBS 新包自动进入库存，版本规则另行审阅。可从只读快照生成候选：

```sh
PYTHONPATH=backend python -m tracker.package setup 包名 \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3 \
  --output /tmp/new-package-proposal.json
```

候选保留 Source0 与 SPEC 哈希；当前自动提案仅覆盖证据明确的 crates 注册表地址。`entry=null` 表示证据不足，不是成功。仓库迁移、同名项目、monorepo、发布线不明均留待审阅，不无人值守合并。Git 规则需额外核对当前源码 tag、项目身份和正式发布约定；能返回新版不等于规则正确。

审阅后写入 `versions/nvchecker.toml`，运行检查，再通过 `plan/apply` 推广到新的外部配置目录。共享字段按展开后的每个 track 检测运营配置冲突；迁移会移除新目录中的旧规则文件，保留原运行目录供回滚。

## 推广到运行配置

保留修改前的配置为 `BASE`，修改副本为 `CANDIDATE`，运营配置为 `RUNTIME`。
从仓库根目录运行（各路径均指向相应目录的 tracker.toml）：

```sh
PYTHONPATH=backend python -m tracker.package plan \
  --base-config "$BASE/tracker.toml" --config "$CANDIDATE/tracker.toml" \
  --runtime-config "$RUNTIME/tracker.toml" --db "$SNAPSHOT_COPY" \
  --output "$REVIEW" --format human
PYTHONPATH=backend python -m tracker.package apply \
  --review "$REVIEW" --runtime-config "$RUNTIME/tracker.toml" \
  --destination "$PREPARED" --format human
```

先审阅 REVIEW 中的文件差异和 review.json。PREPARED 必须尚不存在；apply
生成新目录，不切换线上挂载。部署与回滚见 [部署说明](../docs/deployment.md)。
正常目录配置推广保留未修改文本、注释和组名；冲突停止，不能靠重新分组消除。
旧格式转为目录格式属于迁移，应独立审阅，不混入普通规则修正。


## 自动匹配与优先级

解析顺序：`包名.toml` > 规则组 `packages` 显式成员 > 自动推导。
加载时显式成员展开为包名到原生规则的字典，再由单包文件整条覆盖；
采集器只消费统一的原生 nvchecker 规则，不区分“自定义”执行路径。
规则来源另行保存，供 `package explain` 定位编辑入口。

自动规则在组内声明 `match_prefix`、`match_source`。前缀仅缩小范围；
身份必须由已保存的原生 RPM 展开 Source0 核实，包括当前版本和 SPEC Name。
不执行 SPEC、不仅凭包名猜测上游。同级多个自动规则命中时报错。
没有匹配时保持 Untracked，不影响其他包信息。

检查自动规则需给 `package explain` / `package check` 传入
`--db /path/to/snapshot-copy.sqlite3`；推广时 `package plan` 同样传入该快照，
才能比较完整的有效规则，而非仅显式配置。快照是证据，不是第二个规则编辑入口。

目录名与原生 SPEC Name 不一致属于打包问题。临时单包兼容规则标记
`TODO(drop)`，待上游修复且替代规则验证成功后删除；普通的上游名称别名不属于此类问题。

## 正式发布与旁路

主比较默认只追踪正式发布。需要预发布时，单独定义 `widget@prerelease.toml`，
然后在 groups.toml 中关联；不要把 watch 写入 tracker.toml 的 packages 表：

```toml
[binding.widget]
watch = ["widget@prerelease"]
```

组名应表达共同身份、渠道或维护策略，如 `rocm`、`vulkan_sdk`，而非仅因字段相同
就合并发布线。修改组后，plan 列出有效受影响 track 和被单包覆盖保护的成员。

`--format json`（默认）保留完整机器接口。`--format human` 只显示位置、状态和
报告路径；`check --output FILE` 的完整证据保存在指定文件中。
