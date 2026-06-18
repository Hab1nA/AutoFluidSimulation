# Fluent 仿真数据采集与五项研究指标计算报告

面向液氧甲烷发动机喷注器优化设计的后处理落地方案

| 项目 | 内容 |
| --- | --- |
| 研究对象 | 液氧/甲烷推力室及直流式双流体同轴喷注器算例 |
| 已验证环境 | 工作站 `172.17.135.240`，ANSYS Fluent 2024 R1 v241，PyFluent 0.37.2 |
| 已验证数据 | `D:\xkz_1020\model_gen4_1.cas.h5` 与自动配套读取的 `model_gen4_1.dat.h5` |
| 自动化文件 | `executor/remote_scripts/postprocess_metrics_gen4.py`、`compute_metrics_gen4.py`、`metrics_export_gen4.jou` |
| 输出目标 | `Isp`、`eta_c`、`phi_mean/phi_std`、`Twall_total`、`Tmax_throat/Tmax_sidewall` |

## 1. 方案结论

当前方案不再把四五百万个单元或大量面元导出后再做主计算。最终路径是：

1. PyFluent 启动 Fluent 2024 R1 solver，并读取 `case/data`。
2. Fluent 内部完成必须的 surface integral、volume integral、facet max/min、area-weighted average 和 Named Expression 求和。
3. Fluent 只输出小型标量表 `metrics_reports.csv`。
4. Python 脚本 `compute_metrics_gen4.py` 读取标量表，计算五项研究指标和质量守恒等质量控制量。
5. 大规模 CSV 导出只作为诊断回退，不是最终指标计算的默认路径。

这与三点修正建议对齐：

- 壁面平均温度和最大温度只统计侧壁面：`wall_chamber`、`wall_nozzle`、`wall_throat`。`wall_top`、`wall_gap`、`s------6` 不进入 `Twall` 和 `Tmax` 指标。
- 等效混合比只统计燃烧室，即喉部以前的流体域。脚本默认用 `wall_throat` 上 `x-coordinate` 的最小值作为 `chamber_x_max`，并在 Fluent Expression 中筛选 `Position.x <= chamber_x_max`。
- 推力、`Q_actual`、等效混合比和壁面温度均采用 Fluent 积分或等价面积/体积加权求和，不使用“平均值简单乘面积/体积”的近似主路径。

## 2. 当前算例命名与数据边界

已在真实算例中确认的关键 zone：

| 角色 | 当前名称 | 用途 |
| --- | --- | --- |
| 氧化剂入口 | `inlet_oxidizer` | 入口质量流量 |
| 燃料入口 | `inlet_fuel` | 入口质量流量、理论热输入 |
| 出口 | `outlet` | 推力积分、出口质量流量 |
| 反应流体域 | `s------6.5076` | 热释放体积分、混合比体积积分 |
| 侧壁面 | `wall_chamber`、`wall_nozzle`、`wall_throat` | 壁温指标 |
| 排除壁面 | `wall_top`、`wall_gap`、`s------6` | 仅诊断，不进入五项指标 |

已验证的 Fluent Expression 变量：

- `StaticTemperature`
- `MeanMixtureFraction`
- `Position.x`

已验证的 Fluent report 字段：

- `heat-release-rate`
- `temperature`
- `x-coordinate`
- `x/y/z-velocity`
- `x/y/z-face-area`
- `face-area-magnitude`
- `density`
- `pressure`

## 3. 输出文件

单算例后处理输出目录包含：

| 文件/目录 | 内容 |
| --- | --- |
| `metrics_reports.csv` | Fluent 直接输出的标量报告值 |
| `metrics_summary.csv` | Python 计算后的最终五项指标与质量控制量 |
| `fluent_reports/` | 每个 Fluent report 的原始文本，便于追溯单位和 Net 值 |
| `run/` | Fluent transcript 和运行目录 |

最终计算只依赖 `metrics_reports.csv`。`exit_surface.csv`、`wall_faces.csv`、`chamber_cells.csv` 不再是默认必需文件。

## 4. 五项指标计算步骤

### 4.1 比冲 `Isp`

Fluent 先报告入口质量流量：

```text
mdot_oxidizer = mass_flow_rate(inlet_oxidizer)
mdot_fuel     = mass_flow_rate(inlet_fuel)
mdot_outlet   = mass_flow_rate(outlet)
```

Python 取入口绝对值：

```text
mdot_total = abs(mdot_oxidizer) + abs(mdot_fuel)
```

推力在 Fluent 中通过自定义场函数和出口面积分得到。默认推力轴为 `x`，可通过 `--thrust-axis x|y|z` 调整。

Fluent CFF：

```text
velocity_flux = x_velocity*x_face_area
              + y_velocity*y_face_area
              + z_velocity*z_face_area

cff_thrust_momentum_x =
  density*x_velocity*velocity_flux/(face_area_magnitude+1e-30)

cff_thrust_pressure_x =
  (pressure - pa)*x_face_area/(face_area_magnitude+1e-30)
```

Fluent surface integral：

```text
F_momentum = Integral(outlet, cff_thrust_momentum_x)
F_pressure = Integral(outlet, cff_thrust_pressure_x)
F_total    = F_momentum + F_pressure
```

由于 Fluent 的 surface integral 会再乘以面元面积，这等价于逐面元求和：

```text
F_momentum = sum(rho_i * u_axis,i * (v_i dot A_i))
F_pressure = sum((p_i - pa) * A_axis,i)
```

Python 最后计算：

```text
Isp = F_total / (mdot_total * 9.80665)
```

### 4.2 燃烧效率 `eta_c`

实际热释放功率由 Fluent 对反应流体域做体积分：

```text
Qdot_actual = VolumeIntegral(s------6.5076, heat-release-rate)
```

Python 使用甲烷燃料质量流量计算理论热输入：

```text
Qdot_theoretical = abs(mdot_fuel) * LHV_CH4
LHV_CH4 = 50,000,000 J/kg
eta_c = Qdot_actual / Qdot_theoretical
```

这里的 `Qdot_actual` 是 Fluent 体积分结果，不是先导出体平均热释放率再乘总体积。

### 4.3 等效混合比 `phi_mean` 与 `phi_std`

等效混合比只统计燃烧室，不统计喷嘴。当前实现用喉部边界确定燃烧室下游截断位置：

```text
chamber_x_max = FacetMin(wall_throat, x-coordinate)
```

注意 Fluent 报告 `x-coordinate` 时可能使用当前显示长度单位，例如 `[mm]`。脚本会读取 report 单位并转换为米，再写入 Fluent Expression：

```text
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
phi_hot_volume =
  Sum(IF(StaticTemperature > Tcomb,
         IF(Position.x <= chamber_x_max, 1, 0), 0),
      ['s------6.5076'], Weight="Volume")

phi_sum =
  Sum(IF(StaticTemperature > Tcomb,
         IF(Position.x <= chamber_x_max, phi, 0), 0),
      ['s------6.5076'], Weight="Volume")

phi2_sum =
  Sum(IF(StaticTemperature > Tcomb,
         IF(Position.x <= chamber_x_max, phi*phi, 0), 0),
      ['s------6.5076'], Weight="Volume")
```

Python 只做标量组合：

```text
phi_mean = phi_sum / phi_hot_volume
phi_std  = sqrt(phi2_sum / phi_hot_volume - phi_mean^2)
```

这不是 Fluent 体平均值乘体积，而是 Fluent 对 `phi` 与 `phi^2` 在筛选后的燃烧室高温区逐单元体积加权求和。

### 4.4 侧壁平均温度 `Twall_total`

只考虑侧壁面：

```text
sidewalls = wall_chamber + wall_nozzle + wall_throat
```

Fluent 报告：

```text
wall_area   = Area(sidewalls)
Twall_total = AreaWeightedAverage(sidewalls, temperature)
```

Fluent 的 `area_weighted_avg` 等价于：

```text
Twall_total = Integral(T_wall dA) / Integral(dA)
```

因此这里也是面积加权积分形式，不是把不相关壁面平均后再简单组合。`wall_top`、`wall_gap`、`s------6` 不参与该指标。

### 4.5 最大壁温 `Tmax`

当前输出两个最大温度：

```text
Tmax_throat   = FacetMax(wall_throat, temperature)
Tmax_sidewall = FacetMax(wall_chamber + wall_nozzle + wall_throat, temperature)
```

`Tmax_sidewall` 只在侧壁面中取最大值，不统计 `wall_top`、`wall_gap`、`s------6`。如需排查非侧壁热点，可另行输出诊断项，但不应混入研究指标。

## 5. 自动化执行方式

在工作站 PyFluent 环境中执行：

```bat
C:\ProgramData\anaconda3\Scripts\conda.exe run -n pyfluent python ^
  D:\xkz_1020\temp\codex_metrics_e2e\final_scripts\postprocess_metrics_gen4.py ^
  --case-data D:\xkz_1020\model_gen4_1.cas.h5 ^
  --output-dir D:\xkz_1020\temp\codex_metrics_e2e\final_output_integral4 ^
  --compute-script D:\xkz_1020\temp\codex_metrics_e2e\final_scripts\compute_metrics_gen4.py ^
  --processor-count 1 ^
  --ambient-pressure 0 ^
  --tcomb 1000 ^
  --thrust-axis x
```

批量任务中建议由调度脚本为每个构型设置独立 `output-dir`，并把 `metrics_summary.csv` 汇总进训练标签表。

## 6. 真实算例验证结果

已在 `172.17.135.240` 上完成 E2E 后处理验证。最终输出目录：

```text
D:\xkz_1020\temp\codex_metrics_e2e\final_output_integral4
```

关键 `metrics_reports.csv`：

| 名称 | 数值 |
| --- | ---: |
| `mdot_oxidizer` | `2.598407` |
| `mdot_fuel` | `1.2282464` |
| `mdot_outlet` | `-0.053171835` |
| `qdot_actual` | `-5359564.4` |
| `F_momentum` | `-2.157577e-06` |
| `F_pressure` | `0.0015261056` |
| `wall_area` | `0.16336823` |
| `Twall_total` | `1916.1349` |
| `Tmax_sidewall` | `3193.0088` |
| `Tmax_throat` | `2553.9829` |
| `chamber_x_max` | `-0.017418265 m` |
| `phi_hot_volume` | `0.0024028581` |
| `phi_sum` | `0.0017108348` |
| `phi2_sum` | `0.0035884952` |

对应 `metrics_summary.csv` 中五项指标：

| 指标 | 数值 |
| --- | ---: |
| `Isp` | `4.060975333665601e-05 s` |
| `eta_c` | `-0.08727181125871812` |
| `phi_mean` | `0.7119999304161989` |
| `phi_std` | `0.9932189821649778` |
| `Twall_total` | `1916.1349 K` |
| `Tmax_throat` | `2553.9829 K` |
| `Tmax_sidewall` | `3193.0088 K` |

本次验证后没有残留 `fluent.exe` 进程。

## 7. 质量控制与风险

本次真实算例的数值本身不适合直接作为训练标签使用，原因是质量守恒和热释放表现异常：

```text
mass_imbalance = 0.9861048729942461
Qdot_actual    = -5359564.4 W
eta_c          = -0.08727181125871812
```

这更像是算例未收敛、数据文件不是最终求解结果、出口边界/轴向定义不匹配，或流场状态不适合计算性能标签。脚本已经能稳定获得指标，但批量入库前应设置质量门槛：

| 检查项 | 建议 |
| --- | --- |
| 质量守恒 | `mass_imbalance` 超过课题阈值时标记 suspect |
| 热释放 | `Qdot_actual <= 0` 或 `eta_c` 明显异常时不进入训练集 |
| 推力轴向 | 若几何轴向不是 x，通过 `--thrust-axis` 切换并复核出口动量方向 |
| 燃烧室截断 | 当前用 `wall_throat` 最小 x 坐标推断喉部以前区域；长期建议在几何/网格阶段显式命名燃烧室体积或建立稳定的 chamber region |
| 壁温边界 | 若壁面是固定温度边界，`Twall` 不适合作为热负荷标签，应改用热流或 CHT 温度 |

## 8. 后续建议

1. 在几何/网格流程中增加显式燃烧室体区域或稳定截断面，替代通过 `wall_throat` 坐标推断燃烧室。
2. 统一确认发动机轴向与 Fluent 面法向约定，必要时把 `--thrust-axis` 写入构型配置。
3. 批量后处理前先跑一个收敛良好的基准算例，确认 `mdot_outlet`、`Qdot_actual`、`Isp` 的数量级合理。
4. 在训练标签汇总脚本中保留 `mass_imbalance`、`hot_volume`、`chamber_x_max`、`Tmax_sidewall` 等质量控制字段。
