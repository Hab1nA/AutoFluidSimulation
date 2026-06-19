# Fluent 仿真数据采集与五项研究指标计算报告

面向液氧甲烷发动机喷注器优化设计的后处理落地方案。

| 项目 | 内容 |
| --- | --- |
| 研究对象 | 液氧/甲烷推力室及直流式双流体同轴喷注器算例 |
| 已验证环境 | 工作站 `172.17.135.240`，ANSYS Fluent 2024 R1 v241，PyFluent 0.37.2 |
| 已验证数据 | `D:\xkz_1020\case\model_gen4_1.cas.h5` 与 `model_gen4_1.dat.h5`，1000 代接近稳定结果 |
| 自动化文件 | `executor/remote_scripts/postprocess_metrics_gen4.py`、`compute_metrics_gen4.py` |
| 程序接入口 | `batch_solver_gen4.py` 求解后自动调用；`batch_postprocess_gen4.py` 独立后处理路径也可调用 |
| 输出目标 | `Isp`、`eta_c`、`cstar_efficiency`、`phi_mean/phi_std`、`Twall_total`、`Tmax_sidewall`，并保留质量守恒等 QC 字段 |

## 1. 方案结论

当前方案不把四五百万个单元或大量面元导出后再做主计算。最终路径是：

1. PyFluent 启动 Fluent 2024 R1 solver，并读取 `case/data`。
2. Fluent 内部完成 surface integral、volume integral、facet max/min、area-weighted average 和 Named Expression 求和。
3. Fluent 只输出小型标量表 `metrics_reports.csv`。
4. Python 脚本 `compute_metrics_gen4.py` 读取标量表，组合得到五项研究指标和质量控制量。
5. 大规模 CSV 导出只作为诊断回退，不是最终指标计算的默认路径。

关键边界约定：

- 当前院所实测数据与仿真设置均为半流量工况；后处理结果中的质量流量、推力、热释放功率对应该半流量工况。`Isp`、`eta_c`、`cstar_efficiency`、`phi` 等比值类指标不需要因半流量额外缩放。
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
| `metrics_reports.csv` | Fluent 直接输出的标量报告值 |
| `metrics_summary.csv` | Python 计算后的最终指标与质量控制量 |
| `fluent_reports/` | 每个 Fluent report 的原始文本，便于追溯单位和 Net 值 |
| `run/` | Fluent transcript 和运行目录 |

程序主流程中，求解后指标默认输出到：

```text
<result_dir>\metrics\model_gen4_<config_id>\metrics_summary.csv
```

当前配置下即：

```text
D:\xkz_1020\case\metrics\model_gen4_<config_id>\metrics_summary.csv
```

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

### 4.2 原热释放效率 `eta_c`

实际热释放功率由 Fluent 对反应流体域做体积分：

```text
Qdot_actual = VolumeIntegral(s------6.5076, heat-release-rate)
```

理论热输入使用甲烷低热值：

```text
Qdot_theoretical = abs(mdot_fuel) * LHV_CH4
LHV_CH4          = 50,000,000 J/kg
eta_c            = Qdot_actual / Qdot_theoretical
```

这里的 `Qdot_actual` 是 Fluent 体积分结果，不是先导出体平均热释放率再乘总体积。

该字段继续保留，用于沿用原方案中的热释放诊断口径。但在当前 PDF/flamelet 反应模型和富燃工况下，`heat-release-rate` 体积分不一定能直接代表甲烷低热值完全释放比例，因此它不再作为唯一的燃烧效率判断。

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
chamber_pressure_gauge       = AreaWeightedAverage(wall_chamber, pressure)
chamber_pressure_abs         = chamber_pressure_gauge + pressure_reference
pressure_reference           = 101325 Pa
cstar_reference              = 1830.4 m/s
```

`cstar_reference = 1830.4 m/s` 来自当前任务书/CEA 参考工况。当前实现中的燃烧室压力采用 `wall_chamber` 面积加权平均表压加 `101325 Pa` 作为可复现代理值；前期探索中 Fluent Named Expression 对燃烧室体域内 `pressure`、`StaticPressure`、`AbsolutePressure` 的体积表达式均无法稳定求值。后续若几何/网格阶段提供明确的燃烧室体区域或 cell register，可替换为燃烧室体积平均绝压。

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
  --cstar-reference 1830.4
```

程序接入状态：

- `REMOTE_SCRIPT_FILES` 已包含 `postprocess_metrics_gen4.py`、`compute_metrics_gen4.py`，会随远程脚本部署上传到 `scripts_dir`。
- 主流水线的 `batch_solver_gen4.py` 在求解、保存 case/data、执行原有后处理 journal、关闭 Fluent 会话后，调用 `postprocess_metrics_gen4.py` 生成指标；随后才写 `postprocess_done` flag。
- 独立后处理路径 `batch_postprocess_gen4.py` 也支持同样的 `--metrics-script` 参数，可用于只对已有 case/data 补算指标。
- 主流水线和独立后处理路径都会传入默认 `--metrics-exit-to-throat-area-ratio 7.427276607` 与 `--metrics-cstar-reference 1830.4`，因此新指标可直接进入后续程序化后处理。
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

`metrics_summary.csv` 中关键指标：

| 指标 | 数值 |
| --- | ---: |
| `F_momentum` | `10036.0136 N` |
| `F_pressure` | `915.9470 N` |
| `F_total` | `10951.9606 N` |
| `Isp` | `291.8449 s` |
| `Qdot_actual` | `22342328 W` |
| `Qdot_theoretical` | `61412320 W` |
| 原热释放效率 `eta_c` | `0.3638086` |
| 燃烧室绝压代理 `chamber_pressure_abs` | `1940986.3 Pa` |
| 实际特征速度 `cstar_actual` | `1732.2283 m/s` |
| 参考特征速度 `cstar_reference` | `1830.4 m/s` |
| 特征速度燃烧效率 `cstar_efficiency` | `0.9463660` |
| `phi_mean` | `0.7094508` |
| `phi_std` | `0.8556295` |
| `Twall_total` | `2871.2415 K` |
| `Tmax_sidewall` | `4368.9741 K` |

该结果与院所半流量设定一致。若以后需要换算到全发动机总推力，在几何/边界确认为严格半模型后，可对总推力和质量流量做 2 倍工程换算；`Isp`、`eta_c`、`cstar_efficiency` 等比值指标不随该换算改变。

两种燃烧效率口径的解释：

- `eta_c = 0.3638` 是原热释放体积分诊断值，说明当前 Fluent `heat-release-rate` 对低热值理论热输入的积分比例偏低。
- `cstar_efficiency = 0.9464` 是新增特征速度效率。结合当前 `Isp = 291.8449 s` 与 CEA 参考比冲约 `290.95 s`，该指标更符合发动机性能层面的燃烧效率判断。

## 7. 质量控制与风险

批量入库前建议保留以下质量门槛：

| 检查项 | 建议 |
| --- | --- |
| 质量守恒 | `mass_imbalance` 超过课题阈值时标记 suspect；当前 1000 代结果约 `2.1%` |
| 出口压力 | Fluent `pressure` 为表压，若出口读数为负，应先加 `101325 Pa` 得到绝压后再计算压力推力 |
| 推力轴向 | 当前按 `x` 轴；若几何轴向变化，通过 `--thrust-axis` 切换并复核出口轴向速度 |
| 燃烧室截断 | 当前用 `wall_throat` 最小 x 坐标推断喉部以前区域；长期建议在几何/网格阶段显式命名燃烧室体积或稳定截断面 |
| 特征速度效率 | 当前 `chamber_pressure_abs` 使用 `wall_chamber` 面平均压力代理；如后续得到稳定燃烧室体域压力积分，应替换该压力源并重算 `cstar_efficiency` |
| 壁温边界 | 当前壁温数值偏高，若壁面边界本身不代表真实热结构温度，应考虑热流或 CHT 温度作为后续标签 |

## 8. 后续建议

1. 在训练标签汇总脚本中扫描 `result_dir/metrics/model_gen4_<id>/metrics_summary.csv`，并保留 `mass_imbalance`、`hot_volume`、`chamber_x_max`、`Tmax_sidewall` 等 QC 字段。
2. 在配置文件中显式加入后处理参数时，优先暴露 `metrics_thrust_axis`、`metrics_tcomb`、`metrics_pressure_reference`、`metrics_exit_to_throat_area_ratio`、`metrics_cstar_reference`。
3. 长期在几何/网格流程中增加显式燃烧室体区域或稳定截断面，替代通过 `wall_throat` 坐标推断燃烧室。
