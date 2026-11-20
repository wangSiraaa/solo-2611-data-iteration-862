# obsdelivery — 观测语义服务与十进制交付库

纯 Python 3.11 + 标准库（`sqlite3`、`fractions.Fraction`）。不依赖宿主浮点：
二进制位型解释、参考核对、系数取整、误差判定全部用精确有理数完成。

```
obsdelivery/
  semantics.py      位型解释 + 参考判定（纯函数，无存储）
  service.py        语义服务：观测接纳、参考版本、报告、规范观测产品
  delivery_algo.py  十进制表示选择（纯函数，无存储）
  contracts.py      独立读者合同校验与身份
  delivery.py       交付任务：产品 × 样品集 × 合同，全有或全无
  public.py         公开产品/交付文档 + 独立读回校验
  endpoint_algo.py  复测终点：有效票中位数（纯函数，无存储）
  retest.py         复测服务：位置登记、测量接纳、终点冻结快照
  planner_algo.py   联合门槛优化 + 小样穷举核对（纯函数，无存储）
  desk.py           质检计划台：线配置、缺证标记、不可变计划
  storage.py        SQLite schema（不可变表 + 显式事务）
  demo.py           跑 materials/739 + J0/J1
  demo_qc.py        跑复测终点 + 计划台全流程（含小例与两线取舍）
tests/              精确断言、失败原子性、规模测试
```

运行：

```
python3 -m obsdelivery.demo          # 人读输出
python3 -m obsdelivery.demo --json   # 结构化输出
python3 -m obsdelivery.demo --db /tmp/obs.sqlite
python3 -m obsdelivery.demo_qc       # 复测终点 + 质检计划台演示
python3 -m pytest -q                  # 需 pytest；用 venv 安装即可
```

---

## 1. 位型语义

32 位格式：bit31 sign，bits30..23 exponent，bits22..0 fraction。

| 条件 | 精确值 |
|---|---|
| exp=0, frac=0 | 带符号零（`sign` 原样保留），值 0 |
| exp=0, frac>0 | `(-1)^sign · frac · 2^-149` |
| 1..254 | `(-1)^sign · (2^23+frac) · 2^(exp-150)` |
| 255, frac=0 | 带符号无穷 |
| 255, frac>0 | NaN，payload = fraction 原值 |

`interpret(bits)` 返回 `kind`（`FINITE/ZERO/INFINITY/NAN`）、`bits`、`sign`、
NaN `payload`，有限时返回精确 `Fraction` 值。零的符号与原件身份由 `bits`
与 `sign` 共同保留；负零的交付证据里明确注明“整数系数无法携带 −0，
身份由 original_bits/sign 保留”。

参考判定只看**原值**：

* 有限且 `|value − expected| ≤ tolerance` → `PASS`，否则 `OUTSIDE`；
* 非有限 → `NONFINITE`；无观测 → `MISSING`；
* 报告保存 `abs_difference` 精确差，永不使用交付舍入值。

## 2. 观测接纳

* 按 `(sample_id, revision)` 追加，最高 revision 生效；
* 同版同 bits → 重放（不写新状态）；同版异 bits → **整批拒绝**（`BatchRejected`），
  批次与全部成员以 `rejected` 记入审计，不触碰任何观测；
* 低 revision 合法 → 接纳但 `ignored_older`，不覆盖高版；
* 拒绝是按整个请求批次原子的，`s9` 等未被冲突的记录也不会写入。

## 3. 语义产品与报告

`report()` 把当时的参考版本 + 生效观测固化为不可变行（内容摘要自校验）。
`publish_semantic_product()` 生成独立于交付的规范观测产品，可由服务单独查询。
旧报告/旧产品在新观测、新参考出现后仍按原内容读回，摘要一致；同 id 异内容
直接冲突。参考的派生版本记录 `parent_version`，“复制旧完整版本、替换所列项、
保留未列项”可复现。

## 4. 交付：独立读者合同

合同字段：非空 `allowed_scales ⊆ {0..9}`、正 `max_abs_coefficient`、
非负有理 `max_abs_error`。读者值 = `coefficient / 10^scale`。

对每个允许 scale `s`：

```
coef  = nearest_integer(value · 10^s)      # 精确运算
tie   = 恰好 .5 时取偶系数                 # round_half_even
合法  ⇔  |coef| ≤ max_abs_coefficient  且  |coef/10^s − value| ≤ max_abs_error
```

**scale 选择策略**：按合同给定的 `allowed_scales` 顺序（通常升序）逐个精确
尝试，取第一个合法者。合法的较小 scale 给出更短系数；同一值在多个 scale
都合法时，先出现者胜出。这是有意的文档化策略，不宣称最优体积/精度。
关键点是：**每个**允许 scale 都被精确评估过，绝不会因为只试一个 scale 就拒绝。

**误差证据**：每行保存精确 `error = coefficient/10^scale − original`（带符号）、
`|error|`、全部 scale 的尝试记录（系数、误差、两个界是否满足）以及所选用
系数/scale。`public.verify_public_delivery` 无需数据库即可重算系数界、误差界、
reader 值与状态一致性。

**非有限不能伪造**：`NONFINITE`（NaN/±Inf）与 `MISSING` 没有有限十进制表示，
任一被请求记录无合法表示 → 整个任务拒绝，`deliveries`/`delivery_line` 零写入。
失败只在 `delivery_attempts` 留审计行，不占用成功 `delivery_id`；同一 id 在失败后
可用更合适的合同成功提交。

**OUTSIDE 不会被洗白**：交付行的 `delivered_status` 始终等于原语义 `status`，
即使所交付的十进制恰好等于 `expected`（例如 0.1f 交付成 `100/10^3` 仍是
`OUTSIDE`）。读回校验会检查 `status == delivered_status`。

**身份与重放**：成功 `delivery_id` 绑定 `(产品, 样品集合, 合同)`；同请求重放
返回原内容摘要（`created=False`），异请求抛 `DeliveryIdConflict`。交付行记录
`original_bits`、原精确值/状态、`coefficient`、`scale`、精确误差，独立读回时
核实际表示而非重新选择当前观测。新观测/参考只会生成新的语义产品和新的交付，
旧产品与旧交付保持可重开读。

## 5. 材料与 J0/J1 的实测结果

`demo.py` 实际读取 `materials/739/input.json` 并执行全部操作：

* `3dcccccd`：`13421773/134217728`，比 `1/10` 大 `1/671088640`，零容差 `OUTSIDE`；
* `80000000`：负零，`sign=1`，对 `0` 为 `PASS`；
* `7fc00001`：`NAN`，`payload=4194305`；`7f800000`：正无穷；
* `00000001`：`2^-149`，零容差 `OUTSIDE`；
* 更正后 `s1=1/8`（`3e000000`）且 `r2` 参考 `1/8` → `PASS`；旧报告仍保留
  `3dcccccd` 与 `1/671088640`。

追加请求 `{s1,s2}`、`allowed_scales=[2,3]`、`max_abs_coefficient=99`：

* **J0**（`max_abs_error=0`）整体拒绝：scale 2 → 系数 12、误差 `1/200`（超误差界）；
  scale 3 → 系数 125、误差 0（越系数界）。
* **J1**（`max_abs_error=1/200`）成功：`s1` → `coefficient=12, scale=2`
  （读者值 `3/25`，误差 `−1/200`）；`s2` → `coefficient=0, scale=2`，保留
  `80000000` 与 `sign=1`。J0 被拒不影响语义服务继续查询 `s1` 的 `PASS`。

## 6. 规模

测试覆盖 5000 观测（1000 样品 × 5 版）、100 个参考版本、单次 1000 样品的
语义产品与交付，以及独立读回校验；本地全部 < 2 秒。不做概率统计、浮点求和
或硬件异常模拟。

## 7. 复测终点（可修订，可冻结）

`EndpointService` 按试件归集预先登记的复测位置上的测量：

* `register_plan(specimen, positions, required_count)` 登记不可变复测计划：
  位置名单 + 合同数量（`1..len(positions)`）；同内容重登是重放，异内容冲突。
* 测量按 `(specimen, position, revision)` 接纳，规则与观测接纳一致：
  最高版本生效；同版同内容重放；低版迟到接纳但 `ignored_older`；同版异内容
  **整批拒绝**（审计落库，状态不动）。记录要么是位型值，要么是撤回
  （`retract: true`，不可同时携带 bits）。未登记试件/位置直接 `ValidationError`。
* 每个位置仅其当前有效版本投一票：有效版本是撤回则该票取消；非有限
  （NaN/±Inf）不参加；带符号零是有限值，正常参加。
* `endpoint(specimen)` 给出实时状态：有效位置达到合同数量后，以**全部**
  有效位置的中位数为终点（偶数个取中间两数的精确均值，如 `1` 与 `1+2^-23`
  得 `1+2^-24`，绝不四舍五入）；同时给出所据位型和位置（`basis`）。未达标
  给出缺口：`gap` 及逐位置原因（`missing`/`retracted`/`nonfinite`）。
* `freeze(snapshot_id, specimen)` 把已达标的终点固化为不可变快照；旧快照
  在后续修订后仍按原内容重开（`read_snapshot`），读回时独立重算中位数与
  摘要。同 id 异内容 `SnapshotConflict`；未达标冻结是 `ValidationError`。

## 8. 质检计划台：多线共用复判台的当班门槛

`PlannerDesk` 保存各线配置（不可变，异内容冲突）：校准试件名单及经独立
复核的好坏标签、本班件数、坏件比例允许闭区间、三种单位代价
（`false_accept`/`false_reject`/`review`）。登记时即要求两类都有校准样。

`plan(plan_id, line_ids, shared_quota)` 联合选取当班分选门槛：

* 每线每类内部，登记校准样的终点经验分布就是该类当班分数分布；人工复判
  总正确（复判件只付复判费）。已登记试件缺终点（未登记计划或未达标）→
  抛出 `EvidenceMissing`，**整条线**被标出缺证，并附审计（`desk_attempt`）。
* 每线把本线不同终点（精确有理数，两个不同终点绝不并档）从小到大分档：
  前 `k` 档直放、随后 `m` 档复判、其余退回；允许空段，同分不拆。
* 各线坏件比例在各自闭区间独立变化，故每方案的期望复判量与期望成本都是
  比例的仿射函数，最坏情形必在区间端点（平手取低端点，确定性规则）。
* 约束：对**所有**允许来货构成，预期总复判件数 ≤ 共享额度——即各线最坏
  复判顶点之和 ≤ 额度，精确有理比较，不逐线取整。目标：最坏预期总成本
  最小。求解用逐线 Pareto 前沿合并（精确），并列最优任取一个；
  `brute_force_joint` 提供全乘积穷举核对。
* 交付内容：各线分档、最坏成本及达到它的来货构成、最大复判量及达到它的
  来货构成（两者可以不同），以及各方案在两个端点的期望成本/复判量。
* 每批至多 6 线、每线至多 12 个分数档；比例与代价均须有理数。
* 计划是不可变 artifact：记录所消费的每条线的全部终点，重开
  （`read_plan`）时用存储输入独立重算并校验摘要；修订终点后以同 id 重报
  会 `PlanConflict`，旧计划仍按原内容读回。

任务书小例（`demo_qc` 第 2–4 节实测）：好件终点 0、1，坏件 1、2，当班 4
件、坏件比例恰 1/2，代价 4/2/1/4。共享额度 2 → 只复判分数 1，最坏成本
`1/2`；额度 0 → 分数 1 连同 2 一并退回，成本 `2`；把分数 1 的好件终点
更正到 0 后，零额度最佳成本降为 `0`。两线例（第 3 节）：两线单独配置都
用满额度 2（成本 `1/2`、`1/4`），合并后须取舍，最优 `9/4`（A 线直放 0、
其余退回；B 线复判 1），穷举核对一致。
