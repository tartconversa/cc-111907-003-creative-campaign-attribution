# AI 内容活动的增量贡献与预算续投

在本地合成资料上运行的决策支持应用：用事先登记并冻结的对照安排估计净贡献差异，
把描述性路径与实际增量分开，并把“是否续投、投多少、投哪里”落到可审计的预算动作上。

* 只用 Python 3.14 标准库，不依赖网络与外部账号。
* 来源文件只读；状态、观察与预算动作都写入独立的工作目录。
* 命令输出 JSON，适合脚本化复核；中文完整说明见 [`docs/使用说明.md`](docs/使用说明.md)。

## 快速开始

```bash
python -m campaign_app init --work work --name demo --inputs inputs --at 2026-10-31T00:00:00Z
python -m campaign_app status --workspace work/demo
python -m campaign_app describe --workspace work/demo --cutoff 2026-11-18T00:00:00Z
python -m campaign_app design-create --workspace work/demo --campaign fair \
    --planned-start 2026-11-01T00:00:00Z --planned-end 2026-11-19T00:00:00Z \
    --observation-end 2026-12-01T00:00:00Z --seed 20261101 \
    --allocation-basis "预注册：按相似层基线配对随机，观察前冻结" \
    --maturity-days 7 --max-looks 3 --at 2026-10-31T00:00:00Z
python -m campaign_app review --workspace work/demo --design-id D001 \
    --cutoff 2026-12-01T00:00:00Z --at 2026-12-02T00:00:00Z
python -m campaign_app ledger --workspace work/demo --at 2026-12-02T00:00:00Z
```

完整流程（登记观察计划、决策、续投方案、确认、执行回填、审计）见中文手册。

## 关键设计

* **描述 ≠ 增量**：`describe` 只列触达路径、创意版本、渠道、获客批次与订单净
  贡献的并存关系；不做渠道收入分摊，也不输出“广告带来的增量”。
* **冻结对照**：`design-create` 在观察前冻结合格单位、相似层、分配、主要指标、
  分析口径、成熟期、最大查看次数与观察截止点；分配指纹可复现。
* **守卫阻断**：订单未成熟、分组失衡、渠道覆盖缺口、净贡献缺失、同地区其他活动
  污染、多口径不一致、观察未完成，任一不通过都不能自动判胜。
* **序贯查看**：按登记的最大查看次数做 O'Brien-Fleming 型 alpha 消耗；超出计划
  次数、截止点回退、看完结果后换分组都会被拒绝。
* **诚实版本**：投放后才登记的设计、或看过结果后修改观察计划/主要指标，都会被
  标记为非确认性新决策版本。
* **预算可追溯**：已签约不可取消的合同先扣减；方案确认后才占用；同剩余预算或
  地区名额冲突要求重新比较；下单、取消、执行与迟到退款都留痕。
* **审计链**：状态原子写入，审计日志只追加并带前序哈希；`audit` 可检测改写。

## 环境与资料检查

```bash
python -m unittest discover -s tests -v
```

`tests/test_source_contract.py` 校验来源清单哈希、字段与时间格式；
`tests/test_business_rules.py` 用独立合成夹具覆盖题述业务关系。

容器环境以 Dockerfile 中的公开基础镜像摘要锁定：

```bash
docker build -t local-analytics-inputs .
docker run --rm --network none local-analytics-inputs
```

## 输入契约

来源字段与业务含义见 [`INPUT_CONTRACT.md`](INPUT_CONTRACT.md) 与
[`inputs/schema.json`](inputs/schema.json)。输入不包含预计算建议、处置结果或
规划答案；新增业务状态、使用入口与存储由本应用提供。
