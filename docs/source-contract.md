# 供应商 Git 数据源

配置包含本地仓库路径、允许的产品/网点根和一个分支。先观察 ref 并固定一个 commit，随后只读取该 commit 的不可变对象，不按文件时间增量。products/ 和 outlets/ 下 UTF-8 JSON 含 kind=product|outlet、字符串 id；产品通过 outletIds 引用网点。业务身份为 (kind,id)，文件移动不生成新身份。同一树同键双路径是冲突，即使内容相同也不能静默覆盖。

仅接收普通 blob 模式 100644/100755；符号链接和 gitlink 记录排除原因，不跟随、不执行。树路径以 NUL 分隔读取，允许空格、换行、中文；根外路径拒绝，ref 和路径不进入 shell。多个路径可引用同一 blob，但对象去重不等于业务键去重。禁止 checkout 不可信内容、执行 hooks、展开子模块或使用远程传输。

python -m scripts.git_source DEST 创建合成仓库，给出 initial、moved、rewound 完整 SHA，catalog 是当前分支。快进按祖先关系判断，不按提交时间；缺失后重现与非后代提交是独立观察，未经显式代际确认不得替换当前视图。一次 commit 的商品、网点关系、原文对象及观察水位须完整切换，解析失败或进程终止保留上个完整 commit。

## 接收实现（supplytree acquire）

接收侧状态保存在独立 SQLite 库（默认 `var/acquire/acquire.sqlite3`），与 capture 的 `captures.sqlite3` 分离。核心表：

- `sources`：授权本地仓库、分支、托管根与当前 adoption 水位。
- `observations`：每次观察固定的 commit/tree、上一观察 commit、关系分类、独立代际标记与确认标记。
- `adoptions`：一次完整 commit 接收，关联触发它的 observation。
- `git_blobs`：Git blob SHA、字节数、原文 SHA-256 与内容寻址存储路径。
- `catalog_records`：每次接收内的 `(kind,id)` 业务记录，带 tree、blob 与路径，同接收内业务键唯一。
- `product_outlet_refs`：商品到网点业务键的引用关系。
- `exclusions`：符号链接、gitlink、根外路径、非 UTF-8 路径等排除原因。

命令流程：`add-source` 登记授权仓库 → `observe` 固定并分类 commit → 必要时 `confirm-generation` → `adopt` 原子切换；查询用 `observations`、`current`、`history`、`provenance`。
