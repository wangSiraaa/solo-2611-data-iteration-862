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
  storage.py        SQLite schema（不可变表 + 显式事务）
  demo.py           跑 materials/739 + J0/J1
tests/              精确断言、失败原子性、规模测试
```

运行：

```
python3 -m obsdelivery.demo          # 人读输出
python3 -m obsdelivery.demo --json   # 结构化输出
python3 -m obsdelivery.demo --db /tmp/obs.sqlite
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
