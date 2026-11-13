# 接收流程核验

基线版本只提供原文留存（capture）与单份树检查（trees）。`supplytree acquire` 在此之外实现固定 commit 的业务接收，两者共用同一套只读 Git 访问，互不替代。

## 接收语义

- 每次业务接收先 `observe` 一次分支，把当时解析出的真实 commit SHA 固定到 observations 行；之后源分支再移动，本次接收对象不变（并发 ref 更新不混代）。
- 观察分类完全基于对象祖先关系，不看提交时间：
  - `initial` 首次可见；`up_to_date` 分支仍指向上一观察 commit
  - `fast_forward` 新 commit 是上一观察 commit 的直接后代
  - `multi_commit_jump` 后代但跳过多个 commit（note 给出跳过数量）
  - `rewound` 新 commit 是上一观察 commit 的祖先；`diverged` 互不为后代
  - `disappeared` 分支 ref 无法解析；此前没有任何 commit 时记为 `missing`
- `rewound`、`diverged` 标记 `independent=1`，成为独立观察代际。未显式 `confirm-generation` 前，`adopt` 拒绝替换当前视图，原目录不会被判为全量删除；确认后代际号 +1，视图才整体切换。
- 分支消失后再出现按新观察处理：回到同一 commit 是 `up_to_date`，指向祖先是 `rewound`，均不算普通更新。
- 一次 `adopt` 在单个 SQLite 事务内写入 adoption、catalog_records、product_outlet_refs、git_blobs、exclusions 并推进 sources 水位；解析失败或进程被杀死时全部回滚，关系、原文引用与水位停在上一个完整 commit，恢复后同一对象不重复导入（blob 按 Git SHA 去重，原文文件按 SHA-256 内容寻址）。

## 树与对象规则

- 业务身份为 `(kind,id)`，与路径无关；文件移动不产生新身份。同一树中同键出现在两个路径即冲突，报错并阻止 commit 切换，即使内容相同也不静默覆盖。
- 同一 blob 被多个路径引用时，对象只存一份，但每条来源路径各自保留；不在 products/outlets 托管根内的路径记 `outside-managed-roots` 排除项。
- 仅接收普通 blob（100644/100755）。符号链接记 `symlink-not-followed`，gitlink 记 `gitlink-submodule-not-expanded`，不可解码路径记 `invalid-utf8-path`，根外路径记 `unsafe-path-outside-root`。不 checkout、不运行 hooks、不展开 submodule、不使用远程传输。
- Git 一律以参数数组调用，ref 与路径不进入 shell；树以 NUL 分隔读取，允许空格、换行与非 ASCII 路径，不按行切分。对象 ID 必须匹配 40 位十六进制才会传给 `cat-file`。
- 商品 `outletIds` 必须全部能在同一树的网点集合中解析，否则该 commit 不接收。

## 溯源查询（SQLite）

状态库默认在 `var/acquire/acquire.sqlite3`，原文在其 `blobs/` 目录：

- `acquire history SOURCE --kind product --id P1`：逐次接收列出 commit、tree、blob、路径、observation 与关系。
- `acquire provenance SOURCE --blob <sha>`：一个 Git blob 被哪些接收、哪些路径、哪次观察采用。
- `acquire provenance SOURCE --record <id>`：单条业务记录连同 outlet 引用与原文 SHA-256。
- `acquire current SOURCE`：当前视图的完整商品/网点与排除原因；`acquire observations SOURCE`：观察水位与代际。

## 自动化回归

`tests/test_acquire.py` 是独立端到端回归，全部使用 `scripts/scenarios.py` 在临时目录合成的本地仓库，不接入真实供应商仓库。覆盖：移动后身份不变、同键不同路径冲突、相同 blob 多路径、符号链接/gitlink 排除、坏 JSON 与悬空网点引用、快进/多提交跳跃/回退/分叉/消失重现、并发更新 ref、以及真实子进程 SIGKILL 中断与恢复（`scripts/crash_worker.py`，不是模拟抛异常）。原有 `tests/test_baseline.py` 继续证明基线功能。

- 移动商品不生成新身份；同键双路径冲突阻止 commit 切换
- 固定 commit 后并发更新 ref 不混代，回退需新代际确认
- 进程终止保留旧完整商品/网点关系，怪异路径不按行截断

运行本地服务后可访问 /__requests 核对真实方法、请求头和正文。每次演示用新的临时状态目录；崩溃测试必须启动子进程并实际终止，不只模拟抛异常。正式演示选择上述业务闭环中的一个，控制在 90 秒内；准备依赖和构建在演示前完成。
