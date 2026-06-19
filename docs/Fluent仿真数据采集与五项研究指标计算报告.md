# Fluent 仿真数据采集与五项研究指标计算报告

面向液氧甲烷发动机喷注器优化设计的后处理落地方案。

| 项目 | 内容 |
| --- | --- |
| 研究对象 | 液氧/甲烷推力室及直流式双流体同轴喷注器算例 |
| 已验证环境 | 工作站 `172.17.135.240`，ANSYS Fluent 2024 R1 v241，PyFluent 0.37.2 |
| 已验证数据 | `D:\xkz_1020\case\model_gen4_1.cas.h5` 与 `model_gen4_1.dat.h5`，1000 代接近稳定结果 |
| 自动化文件 | `executor/remote_scripts/postprocess_metrics_gen4.py`、`compute_metrics_gen4.py` |
| 程序接入口 | `batch_solver_gen4.py` 求解后自动调用；`batch_postprocess_gen4.py` 独立后处理路径也可调用 |
| 输出目标 | 以构型名命名的单行 CSV，包含构型信息、必要仿真结果数据和研究指标数据 |

## 1. 方案结论

当前方案不把四五百万个单元或大量面元导出后再做主计算。最终路径是：

1. PyFluent 启动 Fluent 2024 R1 solver，并读取 `case/data`。
2. Fluent 内部完成 surface integral、volume integral、facet max/min、area-weighted average 和 Named Expression 求和。
3. Fluent 只输出小型标量表 `metrics_reports.csv`。
4. Python 脚本 `compute_metrics_gen4.py` 读取标量表，组合得到构型信息、必要仿真结果数据和研究指标数据。
5. 大规模 CSV 导出只作为诊断回退，不是最终指标计算的默认路径。

关键边界约定：

- 当前院所实测数据与仿真设置均为半流量工况；后处理结果中的质量流量、推力、热释放功率对应该半流量工况。`Isp`、`cstar_efficiency`、`phi` 等比值类指标不需要因半流量额外缩放。
- 壁面平均温度和最大温度只统计侧壁面：`wall_chamber`、`wall_nozzle`、`wall_throat`。`wall_top`、`wall_gap`、`s------6` 不进入壁温研究指标。
- 等效混合比只统计燃烧室，即喉部以前的流体域。脚本默认用 `wall_throat` 上 `x-coordinate` 的最小值作为 `chamber_x_max`，并在 Fluent Expression 中筛选 `Position.x <= chamber_x_max`。
- `Q_actual`、等效混合比和壁面温度均采用 Fluent 体积/面积积分或等价加权求和。
- 推力动量项当前采用 Fluent 内置 `mass_flow_rate(outlet)` 与 `mass_weighted_avg(outlet, axis-velocity)` 组合，避免 Fluent CFF 面矢量变量在出口积分中出现错误量级。压力项采用出口面积分，并把 Fluent 表压先加上 `101325 Pa` 后再与环境压力比较。

## 2. 当前算例命名与数据边界

已在真实算例中确认的关键 zone：

| 角色 | 当前名称 | 用途 |
| --- | --- | --- |
| 氧化剂入口 | `inlet_oxidizer` | 入口质量流量 |
| 燃料入口 | `inlet_fuel` | 入口质量流量、理论热输入 |
| 出口 | `outlet` | 出口质量流量、出口轴向速度、压力推力 |
| 反应流体域 | `s------6.5076` | 热释放体积分、混合比体积积分 |
| 侧壁面 | `wall_chamber`、`wall_nozzle`、`wall_throat` | 壁温指标 |
| 排除壁面 | `wall_top`、`wall_gap`、`s------6` | 仅诊断，不进入五项指标 |

已验证的 Fluent 变量/字段包括：`StaticTemperature`、`MeanMixtureFraction`、`Position.x`、`heat-release-rate`、`temperature`、`x-coordinate`、`x/y/z-velocity`、`pressure`、`density`。

## 3. 输出文件

单算例后处理输出目录包含：

| 文件/目录 | 内容 |
| --- | --- |
| `<config_name>.csv` | 面向程序接入的最终单行 CSV，文件名即构型名称 |
| `metrics_summary.csv` | 与 `<config_name>.csv` 内容相同的兼容别名，后续可逐步停用 |
| `metrics_reports.csv` | Fluent 直接输出的标量报告值 |
| `fluent_reports/` | 每个 Fluent report 的原始文本，便于追溯单位和 Net 值 |
| `run/` | Fluent transcript 和运行目录 |

程序主流程中，求解后最终 CSV 默认输出到：

```text
<result_dir>\metrics\model_gen4_<config_id>\model_gen4_<config_id>.csv
```

当前配置下即：

```text
D:\xkz_1020\case\metrics\model_gen4_<config_id>\model_gen4_<config_id>.csv
```

该 CSV 只有一行数据。主流水线会传入 `config_id` 写入 CSV；`config_name` 只用于决定输出文件名，不再作为 CSV 字段。当前标准字段顺序如下：

| 顺序 | 字段 | 单位/类型 | 类别 | 含义 |
| ---: | --- | --- | --- | --- |
| 1 | `config_id` | integer | 构型信息 | 构型编号，例如 `1` |
| 2 | `mdot_oxidizer` | kg/s | 必要仿真结果 | 氧化剂入口质量流量绝对值 |
| 3 | `mdot_fuel` | kg/s | 必要仿真结果 | 燃料入口质量流量绝对值 |
| 4 | `mdot_total` | kg/s | 必要仿真结果 | 推进剂总质量流量，`mdot_oxidizer + mdot_fuel` |
| 5 | `mdot_outlet` | kg/s | QC/追溯 | 出口质量流量绝对值，用于质量守恒检查 |
| 6 | `mass_imbalance` | dimensionless | QC/追溯 | 质量不平衡比例，`abs(mdot_total - mdot_outlet) / mdot_total` |
| 7 | `chamber_pressure_abs` | Pa | 必要仿真结果 | 燃烧室体域平均绝压，由 `chamber_abs_pressure_sum / chamber_volume` 得到 |
| 8 | `throat_area` | m^2 | QC/几何追溯 | 喉部面积，`outlet_area / exit_to_throat_area_ratio` |
| 9 | `cstar_actual` | m/s | 研究指标中间量 | 实际特征速度，`chamber_pressure_abs * throat_area / mdot_total` |
| 10 | `cstar_reference` | m/s | 研究指标参数 | CEA 或试验基准特征速度，当前默认 `1830.4` |
| 11 | `cstar_efficiency` | dimensionless | 研究指标 | 特征速度燃烧效率，`cstar_actual / cstar_reference` |
| 12 | `F_momentum` | N | 必要仿真结果 | 出口动量推力，`abs(mdot_outlet) * abs(u_axis_out)` |
| 13 | `F_pressure` | N | 必要仿真结果 | 出口压力推力，对 `pressure + pressure_reference - ambient_pressure` 做面积分 |
| 14 | `F_total` | N | 必要仿真结果/研究指标输入 | 总推力，`F_momentum + F_pressure` |
| 15 | `Isp` | s | 研究指标 | 比冲，`F_total / (mdot_total * 9.80665)` |
| 16 | `Qdot_actual` | W | QC/热释放诊断 | Fluent 对反应流体域 `heat-release-rate` 的体积分 |
| 17 | `Qdot_theoretical` | W | QC/热释放诊断 | 甲烷低热值理论热输入，`mdot_fuel * 50,000,000` |
| 18 | `phi_mean` | dimensionless | 研究指标 | 燃烧室高温区体积加权等效混合比均值 |
| 19 | `phi_std` | dimensionless | 研究指标 | 燃烧室高温区体积加权等效混合比标准差 |
| 20 | `hot_volume` | m^3 | QC/混合比追溯 | 参与 `phi_mean/phi_std` 统计的燃烧室高温区体积 |
| 21 | `wall_area` | m^2 | QC/壁温追溯 | 侧壁面总面积，统计 `wall_chamber + wall_nozzle + wall_throat` |
| 22 | `Twall_total` | K | 研究指标 | 侧壁面积加权平均温度 |
| 23 | `Tmax_sidewall` | K | 研究指标 | 侧壁最大温度 |

若手动调用 `compute_metrics_gen4.py` 时不传 `--config-id`，`config_id` 字段会省略；主流水线和独立后处理路径都会传入该字段。

## 4. 研究指标计算步骤

### 4.1 比冲 `Isp`

Fluent 先报告入口和出口质量流量：

```text
mdot_oxidizer = mass_flow_rate(inlet_oxidizer)
mdot_fuel     = mass_flow_rate(inlet_fuel)
mdot_outlet   = mass_flow_rate(outlet)
mdot_total    = abs(mdot_oxidizer) + abs(mdot_fuel)
```

动量推力采用出口质量流量与出口轴向质量加权速度：

```text
u_axis_out = mass_weighted_avg(outlet, <axis>-velocity)
F_momentum = abs(mdot_outlet) * abs(u_axis_out)
```

压力推力先把 Fluent 表压转换为绝压，再对出口面积积分：

```text
p_abs_minus_ambient = pressure + pressure_reference - ambient_pressure
pressure_reference  = 101325 Pa
F_pressure          = Integral(outlet, p_abs_minus_ambient)
F_total             = F_momentum + F_pressure
```

默认推力轴为 `x`，可通过 `--thrust-axis x|y|z` 调整。

最终：

```text
Isp = F_total / (mdot_total * 9.80665)
```

### 4.2 热释放诊断量

实际热释放功率由 Fluent 对反应流体域做体积分：

```text
Qdot_actual = VolumeIntegral(s------6.5076, heat-release-rate)
```

理论热输入使用甲烷低热值：

```text
Qdot_theoretical = abs(mdot_fuel) * LHV_CH4
LHV_CH4          = 50,000,000 J/kg
heat_release_ratio = Qdot_actual / Qdot_theoretical
```

这里的 `Qdot_actual` 是 Fluent 体积分结果，不是先导出体平均热释放率再乘总体积。`heat_release_ratio` 可作为诊断量在分析中临时计算，但不再作为最终燃烧效率指标输出。

在当前 PDF/flamelet 反应模型和富燃工况下，`heat-release-rate` 体积分不一定能直接代表甲烷低热值完全释放比例，因此旧的热释放效率字段已从最终指标中删除，燃烧效率统一使用 `cstar_efficiency`。

### 4.3 特征速度燃烧效率 `cstar_efficiency`

新增燃烧效率指标使用特征速度效率衡量：

```text
cstar_actual     = chamber_pressure_abs * throat_area / mdot_total
cstar_efficiency = cstar_actual / cstar_reference
```

其中：

```text
outlet_area                  = Area(outlet)
exit_to_throat_area_ratio    = Ae / At = 7.427276607
throat_area                  = outlet_area / exit_to_throat_area_ratio
chamber_volume               = Sum(IF(Position.x <= chamber_x_max, 1, 0), fluid_zone, Weight="Volume")
chamber_abs_pressure_sum     = Sum(IF(Position.x <= chamber_x_max, AbsolutePressure, 0 [Pa]), fluid_zone, Weight="Volume")
chamber_pressure_abs         = chamber_abs_pressure_sum / chamber_volume
cstar_reference              = 1830.4 m/s
```

`cstar_reference = 1830.4 m/s` 来自当前任务书/CEA 参考工况。当前实现中的燃烧室压力已改为 Fluent Named Expression 体积积分口径；关键是使用 Fluent Expression 变量名 `AbsolutePressure`，并在 `IF` 的非燃烧室分支写成 `0 [Pa]`，否则 Fluent 会因单位不一致而无法求值。`wall_chamber` 面积加权绝压仍可作为 QC 对照字段保留，但不再作为 `cstar_efficiency` 的压力源。

### 4.4 等效混合比 `phi_mean` 与 `phi_std`

等效混合比只统计燃烧室，不统计喷嘴。当前实现用喉部边界确定燃烧室下游截断位置：

```text
chamber_x_max = FacetMin(wall_throat, x-coordinate)
Position.x <= chamber_x_max [m]
```

高温燃烧统计区：

```text
StaticTemperature > Tcomb
Position.x <= chamber_x_max
```

等效混合比使用混合分数：

```text
phi = ((1 - MeanMixtureFraction) / (MeanMixtureFraction + 1e-12)) / 4
```

Fluent Named Expressions 直接做体积加权求和：

```text
phi_hot_volume = Sum(IF(hot, IF(chamber, 1, 0), 0), zone, Weight="Volume")
phi_sum        = Sum(IF(hot, IF(chamber, phi, 0), 0), zone, Weight="Volume")
phi2_sum       = Sum(IF(hot, IF(chamber, phi*phi, 0), 0), zone, Weight="Volume")
```

Python 只做标量组合：

```text
phi_mean = phi_sum / phi_hot_volume
phi_std  = sqrt(phi2_sum / phi_hot_volume - phi_mean^2)
```

这不是 Fluent 体平均值乘体积，而是 Fluent 对 `phi` 与 `phi^2` 在筛选后的燃烧室高温区逐单元体积加权求和。

### 4.5 侧壁平均温度 `Twall_total`

只考虑侧壁面：

```text
sidewalls = wall_chamber + wall_nozzle + wall_throat
```

Fluent 报告：

```text
wall_area   = Area(sidewalls)
Twall_total = AreaWeightedAverage(sidewalls, temperature)
```

`area_weighted_avg` 等价于：

```text
Twall_total = Integral(T_wall dA) / Integral(dA)
```

### 4.6 最大壁温 `Tmax_sidewall`

当前只输出侧壁最大温度：

```text
Tmax_sidewall = FacetMax(wall_chamber + wall_nozzle + wall_throat, temperature)
```

`wall_throat` 仍包含在侧壁集合内，因此喉部热点会进入 `Tmax_sidewall`；但不会再作为单独指标列输出。`Tmax_sidewall` 不统计 `wall_top`、`wall_gap`、`s------6`。

## 5. 自动化执行方式

工作站单算例手动执行示例：

```bat
C:\ProgramData\anaconda3\Scripts\conda.exe run --no-capture-output -n pyfluent python -u ^
  D:\xkz_1020\scripts\postprocess_metrics_gen4.py ^
  --case-data D:\xkz_1020\case\model_gen4_1.cas.h5 ^
  --output-dir D:\xkz_1020\case\metrics\model_gen4_1 ^
  --compute-script D:\xkz_1020\scripts\compute_metrics_gen4.py ^
  --processor-count 1 ^
  --ambient-pressure 0 ^
  --pressure-reference 101325 ^
  --tcomb 1000 ^
  --thrust-axis x ^
  --exit-to-throat-area-ratio 7.427276607 ^
  --cstar-reference 1830.4 ^
  --config-name model_gen4_1 ^
  --config-id 1
```

程序接入状态：

- `REMOTE_SCRIPT_FILES` 已包含 `postprocess_metrics_gen4.py`、`compute_metrics_gen4.py`，会随远程脚本部署上传到 `scripts_dir`。
- 主流水线的 `batch_solver_gen4.py` 在求解、保存 case/data、执行原有后处理 journal、关闭 Fluent 会话后，调用 `postprocess_metrics_gen4.py` 生成指标；随后才写 `postprocess_done` flag。
- 独立后处理路径 `batch_postprocess_gen4.py` 也支持同样的 `--metrics-script` 参数，可用于只对已有 case/data 补算指标。
- 主流水线和独立后处理路径都会传入默认 `--metrics-exit-to-throat-area-ratio 7.427276607`、`--metrics-cstar-reference 1830.4`、`--config-name model_gen4_<id>` 与 `--config-id <id>`；其中 `--config-name` 只控制最终 CSV 文件名，CSV 数据内以 `config_id` 作为构型标识，可直接进入后续程序化后处理。
- 指标 Fluent 默认使用 1 核启动，避免对主求解资源造成明显影响；如需要可通过 `--metrics-processor-count` 调整。

## 6. 真实算例后处理结果

已在 `172.17.135.240` 上对 `D:\xkz_1020\case\model_gen4_1.cas.h5` / `.dat.h5` 完成后处理。当前算例为半流量工况，入口边界与质量流量结果一致：

| 名称 | 数值 |
| --- | ---: |
| `mdot_oxidizer` | `2.598407 kg/s` |
| `mdot_fuel` | `1.2282464 kg/s` |
| `mdot_total` | `3.8266534 kg/s` |
| `mdot_outlet` | `3.7461382 kg/s` |
| `mass_imbalance` | `0.0210406` |
| `O/F` | `2.1155422` |

出口与推力相关结果：

| 名称 | 数值 |
| --- | ---: |
| 出口面积 | `0.025364797 m^2` |
| 喉部面积 | `0.0034150872 m^2` |
| 出口/喉部面积比 | `7.427276607` |
| 出口体积流量 | `67.995314 m^3/s` |
| 出口面积平均密度 | `0.055261495 kg/m^3` |
| 出口面积平均绝压 | `36110.952 Pa` |
| 出口面积平均温度 | `1365.8761 K` |
| 出口面积平均速度模 | `2724.2803 m/s` |
| 出口质量加权 `x` 速度 | `2679.0292 m/s` |
| 出口面积平均 Mach | `3.0388148` |

`model_gen4_1.csv` 中关键字段：

| 指标 | 数值 |
| --- | ---: |
| `F_momentum` | `10036.0136 N` |
| `F_pressure` | `915.9470 N` |
| `F_total` | `10951.9606 N` |
| `Isp` | `291.8449 s` |
| `Qdot_actual` | `22342328 W` |
| `Qdot_theoretical` | `61412320 W` |
| 热释放诊断比 `Qdot_actual / Qdot_theoretical` | `0.3638086` |
| 燃烧室体积 `chamber_volume` | `0.0028444491 m^3` |
| 燃烧室绝压体积分 `chamber_abs_pressure_sum` | `5516.9164 Pa·m^3` |
| 燃烧室体域平均绝压 `chamber_pressure_abs` | `1939537.7 Pa` |
| 壁面压力对照 `chamber_wall_pressure_abs` | `1940986.3 Pa` |
| 实际特征速度 `cstar_actual` | `1730.9356 m/s` |
| 参考特征速度 `cstar_reference` | `1830.4 m/s` |
| 特征速度燃烧效率 `cstar_efficiency` | `0.9456597` |
| `phi_mean` | `0.7094508` |
| `phi_std` | `0.8556295` |
| `Twall_total` | `2871.2415 K` |
| `Tmax_sidewall` | `4368.9741 K` |

该结果与院所半流量设定一致。若以后需要换算到全发动机总推力，在几何/边界确认为严格半模型后，可对总推力和质量流量做 2 倍工程换算；`Isp`、`cstar_efficiency` 等比值指标不随该换算改变。

热释放诊断与燃烧效率的解释：

- `Qdot_actual / Qdot_theoretical = 0.3638` 只作为热释放体积分诊断值，说明当前 Fluent `heat-release-rate` 对低热值理论热输入的积分比例偏低。
- `cstar_efficiency = 0.9457` 是当前保留的燃烧效率指标。结合当前 `Isp = 291.8449 s` 与 CEA 参考比冲约 `290.95 s`，该指标更符合发动机性能层面的燃烧效率判断。

## 7. 质量控制与风险

批量入库前建议保留以下质量门槛：

| 检查项 | 建议 |
| --- | --- |
| 质量守恒 | `mass_imbalance` 超过课题阈值时标记 suspect；当前 1000 代结果约 `2.1%` |
| 出口压力 | Fluent `pressure` 为表压，若出口读数为负，应先加 `101325 Pa` 得到绝压后再计算压力推力 |
| 推力轴向 | 当前按 `x` 轴；若几何轴向变化，通过 `--thrust-axis` 切换并复核出口轴向速度 |
| 燃烧室截断 | 当前用 `wall_throat` 最小 x 坐标推断喉部以前区域；长期建议在几何/网格阶段显式命名燃烧室体积或稳定截断面 |
| 特征速度效率 | 当前 `chamber_pressure_abs` 使用燃烧室体域平均绝压；`chamber_wall_pressure_abs` 只作为壁面压力对照，不参与 `cstar_efficiency` |
| 壁温边界 | 当前壁温数值偏高，若壁面边界本身不代表真实热结构温度，应考虑热流或 CHT 温度作为后续标签 |

## 8. 后续建议

1. 在训练标签汇总脚本中扫描 `result_dir/metrics/model_gen4_<id>/model_gen4_<id>.csv`，并保留 `mass_imbalance`、`hot_volume`、`chamber_x_max`、`Tmax_sidewall` 等 QC 字段。
2. 在配置文件中显式加入后处理参数时，优先暴露 `metrics_thrust_axis`、`metrics_tcomb`、`metrics_pressure_reference`、`metrics_exit_to_throat_area_ratio`、`metrics_cstar_reference`。
3. 长期在几何/网格流程中增加显式燃烧室体区域或稳定截断面，替代通过 `wall_throat` 坐标推断燃烧室。
