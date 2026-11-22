# 住房消费提取资料采集与办理工作台

面向窗口人员的离线工作台：摄入本地合成的申请表、住房证明、发票、政策版本与付款回执，
按城市规则评估材料与额度，记录审核决定与付款进度，并在材料更正/撤回或政策调整时
指出需要重审的未结申请。**所有人员、住房、金额与票据均为虚构合成资料，无需联网、无第三方依赖。**

## 快速开始

需要 Python 3.11+。隔离模式（`-I`）下通过根目录启动器运行（启动器只把可信代码目录
加入 `sys.path`，资料目录从不加入）：

```sh
# 1. 摄入合成资料（可重复执行，按 source_id 幂等）
python3 -I run_workbench.py --store data/wb.json ingest --reset fixtures/sources.json

# 2. 申请总览 / 重审队列 / 家庭额度台账
python3 -I run_workbench.py --store data/wb.json list
python3 -I run_workbench.py --store data/wb.json queue
python3 -I run_workbench.py --store data/wb.json ledger

# 3. 单条申请评估、审批、付款
python3 -I run_workbench.py --store data/wb.json evaluate AP-83
python3 -I run_workbench.py --store data/wb.json decide AP-83 approved --by clerk-zhang
python3 -I run_workbench.py --store data/wb.json pay AP-83 PAY-20

# 4. 审核员追踪（决定 → 政策版本 → 证据哈希）与申请人进度
python3 -I run_workbench.py --store data/wb.json trace AP-81
python3 -I run_workbench.py --store data/wb.json applicant AP-82

# 5. 单文件 HTML 报表
python3 -I run_workbench.py --store data/wb.json html data/report.html
```

测试（含原有基线契约测试，共 38 个）：

```sh
python3 -I -m unittest discover -s tests
```

一键观看完整办理链路（临时库，不污染数据）：

```sh
python3 -I demo/demo_walkthrough.py
```

## 资料信封格式

`fixtures/sources.json` 中每条记录是一个不可变信封：

```json
{"source_id": "invoice-92", "record_kind": "receipt",
 "collected_at": "2026-10-11T10:12:00+08:00",
 "payload": {"receipt_id": "INV-92", "application_id": "AP-81", "...": "..."}}
```

支持的 `record_kind`：`policy`（城市政策版本）、`application`（申请）、
`receipt`（发票，按 `revision` 更正、`state=withdrawn` 撤回）、
`housing_certificate`（住房证明）、`member`（缴存人及账户余额）、
`household`（家庭与成员关系）、`decision`（审核决定）、`payment`（付款回执）。

## 业务规则如何落地

| 业务要求 | 实现方式 |
| --- | --- |
| 城市规则不同（可申请用途、材料、票据窗口、家庭上限） | `fundbench/policy.py` 按城市+生效日期选版；政策信封带 `effective_from` 与版本号 |
| 家庭额度共享，但与个人账户余额不是同一个数 | `fundbench/ledger.py` 按「家庭+年度+用途」汇总有效决定占用；引擎分别校验家庭剩余上限与成员账户余额，取最小值为可批准额 |
| 政策调整只影响未结申请 | 重审队列只纳入非终态申请；已付款申请的决定快照永久冻结当时政策版本与证据 |
| 发票重复提交 | 同一 `receipt_id` 被多个申请引用时，所有引用方都得到 `RECEIPT_DUPLICATE` 硬性问题 |
| 材料更正/撤回后指出重审对象 | 发票/证明保留全部历史信封，队列按「多版本=更正、state=withdrawn=撤回」派生，只列未结申请 |
| 已付款项目不能再次提交/改判 | 付款后申请进入 `paid` 终态，`decide`/`withdraw`/重复付款一律拒绝 |
| 一件材料失效不错误释放他人额度 | 额度只从有效决定派生；材料失效只产生重审提示，必须由审核员作出新决定后台账才重算 |
| 从决定回到政策版本与证据 | 每条决定保存冻结快照：政策摘要、申请哈希、逐项证据的版本/状态/SHA-256 哈希（`trace` 可查） |
| 申请人看待补材料与到账 | `applicant` 视图列待补材料、硬性问题、批准金额与付款回执 |
| 可持续处理 | 摄入按 `source_id` 幂等，新批次随时追加；所有视图从信封重建，存储仅做原子追加 |

## 目录结构

```
fundbench/
  store.py      信封存储与事件日志（JSON 原子写、幂等摄入）
  registry.py   从信封派生的只读视图（人员/家庭/证明/票据/决定/付款）
  policy.py     政策版本归一化与按日期选版
  engine.py     单条申请评估：材料、票据窗口/重复/撤回、金额、额度
  ledger.py     家庭年度额度台账（从有效决定派生）
  workbench.py  门面：摄入、补录、审批、付款、撤回、重审队列、双向视图
  report.py     文本与单文件 HTML 报表
  cli.py        命令行
tests/          38 个 unittest 用例（含基线信封契约）
fixtures/       合成资料（39 个信封，11 个申请、2 城市 3 版政策）
```

设计细节与场景清单见 [docs/DESIGN.md](docs/DESIGN.md)。
