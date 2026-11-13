# supplytree

供应商商品目录 Git 树与不可变对象检查工具。

## 本地运行

使用 Python 3.11 标准库，无 pip 依赖。另需 Git 2.39 或更高版本。所有来源与业务数据都是本地合成数据，不连接真实用户系统。

```sh
python -m scripts.materialize
mkdir -p .tmp
python -m scripts.git_source .tmp/supplier
python -m supplytree trees .tmp/supplier
python -m compileall -q .
python -m unittest discover -s tests -v
python -m scripts.demo
```

检查命令输出单份来源的结构摘要。`capture URL --method GET --state var` 仅接受 loopback HTTP，保存原始响应为 SHA256 文件，并在 SQLite captures 表登记；可用 `--header 'Name:Value'` 传头、`--body FILE` 传正文，不跟随重定向。检查器不执行多页、业务去重或正式接收。

## 业务接收 acquire

`acquire` 在保留 trees/capture 的同时，实现固定到真实 commit 的商品与网点接收，数据源是已登记的授权本地仓库，不使用文件修改时间或远程传输：

```sh
python -m supplytree acquire --state .tmp/acq add-source supplier .tmp/supplier \
    --branch catalog --product-root products --outlet-root outlets
python -m supplytree acquire --state .tmp/acq observe supplier
python -m supplytree acquire --state .tmp/acq adopt supplier
# 回退或分叉产生独立代际时，必须先显式确认
python -m supplytree acquire --state .tmp/acq confirm-generation supplier --observation 3
python -m supplytree acquire --state .tmp/acq current supplier
python -m supplytree acquire --state .tmp/acq history supplier --kind product --id P1
python -m supplytree acquire --state .tmp/acq provenance supplier --blob <git-sha>
```

每次 observe 立即把分支固定为一个 commit SHA 并按祖先关系分类（快进、多提交跳跃、回退、分叉、消失/重现）；非后代提交是独立观察代际，未经 `confirm-generation` 不得替换当前视图。一次 adopt 在单个 SQLite 事务内切换商品、网点引用、原文 blob 与观察水位，解析失败或进程终止都停在上一个完整 commit。仅接收普通 blob，符号链接与 gitlink 记录排除原因；路径以 NUL 分隔读取，ref 与路径不进入 shell。新增能力与现有摘要工具的边界见 docs/acceptance.md。

## 可复现容器

```sh
docker build -t supplytree:local .
docker run --rm --network none supplytree:local python -m unittest discover -s tests -v
docker run --rm --network none supplytree:local
```

镜像固定基础摘要。构建在正式运行前完成；运行和测试不安装包、不下载数据。演示命令实际执行检查器及本地来源，通常不超过 5 秒；它不是新增业务接收已实现的声明。开发完成后扩展 scripts/demo.py 展示一个真实业务闭环，完整输出并控制在 90 秒以内。

## 协议与来源

- docs/source-contract.md 给出支持子集、字段及失败语义。
- docs/acceptance.md 给出业务验收边界。
- fixtures/source.json 是本地可重放 HTTP 路由；`python -m scripts.source_server --port 8765` 启动，`/__requests` 可检查实际请求。
- scripts/materialize.py 将版本化 base64 二进制夹具还原为原始字节，重复执行安全；二进制文件不是用户附件。
- journal.py 只记录原始响应，不代替新增流程自己的事务和恢复设计。
