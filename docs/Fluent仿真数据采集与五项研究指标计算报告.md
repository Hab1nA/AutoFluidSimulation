# Fluent 仿真数据采集与五项研究指标计算报告

面向液氧甲烷发动机喷注器优化设计的后处理方案

| 项目 | 内容 |
| --- | --- |
| 研究对象 | 液氧/甲烷推力室及直流式双流体同轴喷注器算例 |
| 软件环境 | 已在工作站 172.17.135.240 上验证 ANSYS Fluent 2024 R1 v241 与 PyFluent 0.37.2；后处理采用 Fluent journal / PyFluent 导出数据，Python 统一计算指标 |
| 目标输出 | Isp、ηc、φ̄/σφ、Twall、Tmax 五类指标及其伴随质量控制数据 |
| 数据用途 | 构建喷注器几何参数到性能/热负荷/混合均匀性的代理模型训练标签 |

> 资料依据：本报告依据《基于深度学习方法的液氧甲烷发动机喷注器优化设计研究（中期报告）》中第 2.1.2 节“研究指标”、第 2.2 节“技术路径”及第 3.3 节“液氧甲烷发动机燃烧仿真”的定义和模型设置撰写。

## 目录

- [1 目的、指标来源与基本假设](#1-目的、指标来源与基本假设)
- [2 Fluent 中需要预先命名的面、体和场变量](#2-fluent-中需要预先命名的面、体和场变量)
- [3 通用后处理流程与数据文件组织](#3-通用后处理流程与数据文件组织)
- [4 五项研究指标的数据采集与计算方法](#4-五项研究指标的数据采集与计算方法)
- [5 自动化实施建议：Fluent / PyFluent 后处理任务拆分](#5-自动化实施建议-fluent-pyfluent-后处理任务拆分)
- [6 计算结果检查、收敛判据与异常处理](#6-计算结果检查、收敛判据与异常处理)
- [7 推荐输出数据表结构](#7-推荐输出数据表结构)
- [8 结论](#8-结论)
- [附录 A：单算例后处理伪代码](#附录-a-单算例后处理伪代码)
- [附录 B：常见错误及修正方式](#附录-b-常见错误及修正方式)

## 1 目的、指标来源与基本假设

本报告的目的，是把 Fluent 求解得到的流场结果转化为可用于喷注器优化设计的数据标签。由于研究对象为液氧甲烷推力室，喷注器结构变化会同时影响喷注动量分布、混合均匀性、燃烧热释放、出口动量通量以及壁面热负荷，因此后处理不能只导出单一温度或压力量，而应建立一套面向多目标优化的标准化数据采集与计算流程。

中期报告已明确五类研究指标：比冲 Isp、燃烧效率 ηc、等效混合比 φ(x) 及其在燃烧区域内的统计量、推力室壁面平均温度 Twall、推力室壁面最大温度 Tmax。其中 Isp 用于表征推进剂利用效率，ηc 用于表征化学能释放程度，φ̄ 与 σφ 用于表征混合比水平和混合均匀性，Twall 与 Tmax 用于表征总体热负荷与局部热结构风险。

$$
I_{sp} = \frac{F}{\dot{m} g_0}
$$

$$
\eta_c = \frac{E_{actual}}{E_{theoretical}}
$$

$$
\phi(x) = \frac{(O/F)_{actual}}{(O/F)_{stoich}} = \frac{Y_{ox}/Y_{fuel}}{4}
$$

> 说明：中期报告给出液氧甲烷化学计量混合比 (O/F)stoich = 4，并指出实际工程中可采用富燃设计以降低燃烧温度和热风险。因此 φ=1 并不必然是本课题的最优目标，实际优化中更应关注设计约束下的 φ̄ 区间和 σφ 最小化。

| 指标 | 优化方向 | 核心数据源 | 用途 |
| --- | --- | --- | --- |
| Isp | 越大越好 | 喷管出口动量通量与压力推力；总推进剂质量流量 | 性能目标 |
| ηc | 越大越好，但需与热负荷约束协同 | 反应域体积热释放率；甲烷质量流量；甲烷低位热值 | 燃烧完整性目标 |
| φ̄、σφ | φ̄ 作为混合比水平，σφ 越小代表越均匀 | 燃烧高温区内混合分数或氧/燃质量分数；单元体积 | 混合均匀性目标/约束 |
| Twall | 越低越安全，但需结合性能判断 | 喷注器面、燃烧室壁面、喷管壁面温度；面积权重 | 总体热负荷目标 |
| Tmax | 越低越安全 | 喉部及全壁面温度最大值 | 局部热风险约束 |

## 2 Fluent 中需要预先命名的面、体和场变量

为了保证不同喷注器构型的后处理结果具有一致性，几何建模和网格划分阶段应保持边界命名完全统一。建议不要依赖 Fluent 自动生成的 face-id 或 zone-id，因为参数化建模后这些编号可能发生变化。所有自动化脚本应通过边界名称访问数据。

本轮已在 `D:\xkz_1020\case\model_gen4_1.cas.h5` / `model_gen4_1.dat.h5` 上通过 PyFluent 读取真实算例并确认当前 Gen4 模型的命名如下。后续脚本应优先使用“当前算例实际名称”，若几何或网格流程改名，再在配置层做映射，而不要在计算公式中写死 zone-id。

| 抽象角色 | 当前算例实际名称 | Fluent 类型 | 需要导出的数据 | 用途 |
| --- | --- | --- | --- | --- |
| 氧化剂入口 | `inlet_oxidizer` | `mass-flow-inlet` | Mass Flow Rate、入口温度、入口混合分数边界值 | 总流量、O/F 与入口边界核验 |
| 燃料入口 | `inlet_fuel` | `mass-flow-inlet` | Mass Flow Rate、入口温度、入口混合分数边界值 | 总流量、理论热输入 |
| 喷管出口截面 | `outlet` | `pressure-outlet` | `density`、`x-velocity`、`pressure`、`x-face-area`、`face-area-magnitude` | 推力和 Isp 计算 |
| 燃烧室壁面 | `wall_chamber` | `wall` | `temperature`、`wall-temperature`、`heat-flux`、`face-area-magnitude` | Twall 分区统计 |
| 喷管壁面 | `wall_nozzle` | `wall` | `temperature`、`wall-temperature`、`heat-flux`、`face-area-magnitude` | Twall 分区统计 |
| 喉部壁面 | `wall_throat` | `wall` | `temperature`、`wall-temperature`、`heat-flux`、`face-area-magnitude` | Tmax 优先监控 |
| 其他壁面 | `wall_top`、`wall_gap`、`s------6` | `wall` | `temperature`、`heat-flux`、`face-area-magnitude` | 全壁面 Tmax、P99.5 与总壁温统计 |
| 反应流体域 | `s------6.5076` | `fluid` | `temperature`、`fmean`、`heat-release-rate`、`cell-volume` | ηc、φ 统计与体积权重 |

当前 Fluent/PDF 燃烧模型中已经确认的关键场变量为：`temperature`、`density`、`pressure`、`x-velocity`、`fmean`、`heat-release-rate`、`cell-volume`、`face-area-magnitude`、`x-face-area`、`heat-flux`。其中 `fmean` 是 Mean Mixture Fraction，适合作为 φ 统计的优先数据源；`heat-release-rate` 是 Heat Release Rate，可通过 Volume Integral 得到实际热释放功率。

## 3 通用后处理流程与数据文件组织

当前方案将每个 Fluent 算例后处理拆成“读取结果—Fluent 报告量采集—必要场数据导出—Python 统一计算—质量控制归档”五步。设计原则是：Fluent 只负责读取 case/data、执行成熟的 surface/volume integral、导出必要原始场量；五项研究指标的最终公式全部由 `executor/remote_scripts/compute_metrics_gen4.py` 负责，避免在 journal/TUI/Scheme 中堆叠复杂且难测试的数学逻辑。

| 步骤 | 任务 | 采集内容 | 输出 |
| --- | --- | --- | --- |
| 1 | 读取 Fluent 结果 | `model_gen4_<id>.cas.h5` 和对应 `.dat.h5` | 已加载的 solver session |
| 2 | Fluent 报告量采集 | `mdot_oxidizer`、`mdot_fuel`、`mdot_outlet`、壁面面积/平均温度/最大温度、`qdot_actual` | `metrics_reports.csv` |
| 3 | 必要场数据导出 | 出口面、反应流体域、壁面面元的最小字段集合 | `exit_surface.csv`、`chamber_cells.csv`、`wall_faces.csv` |
| 4 | Python 统一计算 | Isp、ηc、φ̄、σφ、Twall、Tmax、质量控制量 | `metrics_summary.csv` |
| 5 | 质量控制与归档 | 质量守恒误差、热释放合理性、高温区体积、壁温极值 | 训练标签与 suspect 标记 |

建议每个算例保留三类结果：`metrics_summary.csv` 存放最终指标，`metrics_reports.csv` 存放 Fluent report 原始数值，`field_export/` 存放出口截面、反应流体域和壁面面元数据。这样既能直接训练代理模型，也能在发现异常标签时回溯原始场量。

## 4 五项研究指标的数据采集与计算方法

### 4.1 比冲 Isp

比冲需要由推力和总推进剂质量流量共同确定。Fluent 中不能简单用出口平均速度乘以质量流量替代推力，因为喷管出口可能存在速度径向分布、非均匀压力分布和局部回流。推荐在 nozzle_exit 截面上进行动量通量与压力推力积分。

| 采集位置 | Fluent 操作/变量 | 符号 | 说明 |
| --- | --- | --- | --- |
| 入口面 ox_inlet_* | Mass Flow Rate | m_dot_ox | Fluent 入口质量流量符号可能为负，后处理取绝对值 |
| 入口面 fuel_inlet_* | Mass Flow Rate | m_dot_fuel | 用于总质量流量和燃烧效率理论热输入 |
| 出口面 nozzle_exit | ρ、u、v、w、p、n_x、dA | 出口面元数据 | 用于外部脚本积分推力；若 Fluent 无法直接导出法向分量，可导出面法向或用几何方向确定 |
| 环境/出口边界 | Ambient pressure 或 back pressure | pa | 压力推力基准；真空工况取 pa=0，地面工况取试验环境压强 |

$$
\dot{m} = |\dot{m}_{ox}| + |\dot{m}_{fuel}|
$$

$$
F_x = \sum_i \rho_i u_{x,i}(\vec{v}_i \cdot \vec{n}_i)\,dA_i + \sum_i (p_i - p_a)n_{x,i}\,dA_i
$$

$$
I_{sp} = \frac{F_x}{\dot{m}g_0}, \quad g_0 = 9.80665\ \mathrm{m/s^2}
$$

当前 Gen4 算例中出口面为 `outlet`，出口截面坐标显示其法向与发动机轴向可按 x 方向处理。因此当前落地脚本采用 `x-face-area` 作为带符号面积分量，计算：

$$
F_{momentum} = \sum_i \rho_i u_{x,i}^2 A_{x,i}
$$

$$
F_{pressure} = \sum_i (p_i - p_a) A_{x,i}
$$

对于出口压力分布接近均匀的情况，压力项可近似为 `(p̄e - pa)Ae`；批量后处理中仍应优先使用面元积分形式，以免不同喷注器构型造成出口流动畸变时引入系统误差。

> 质量守恒检查：应同时比较入口总质量流量和出口质量流量，误差 eps_m = |m_dotin - m_dotout| / m_dotin。稳态算例建议 eps_m < 0.5% 或按课题统一阈值执行；超过阈值的 Isp 不应直接进入训练集。

### 4.2 燃烧效率 ηc

燃烧效率反映实际化学热释放相对于理论化学能输入的比例。Fluent 中应采集反应流体域内的体积热释放率，并对整个反应域积分得到实际热释放功率。理论热输入应以燃料甲烷的质量流量和甲烷低位热值计算。

| 采集位置 | Fluent 操作/变量 | 符号 | 说明 |
| --- | --- | --- | --- |
| `s------6.5076` | `heat-release-rate` | q_dot_chem,i [W/m³] | 当前算例已确认的热释放率字段 |
| `s------6.5076` | `cell-volume` | Vi | 用于外部积分；也可直接使用 Fluent Volume Integral |
| `inlet_fuel` | Mass Flow Rate | m_dot_CH4 | 理论热输入应采用甲烷质量流量 |
| 常数 | LHVCH4 | 50.0 MJ/kg | 与中期报告中的甲烷低位热值保持一致 |

$$
\dot{Q}_{actual} = \int_V \dot{q}_{chem}\,dV \approx \sum_i \dot{q}_{chem,i} V_i
$$

$$
\dot{Q}_{theoretical} = \dot{m}_{CH4} \cdot LHV_{CH4}
$$

$$
\eta_c = \frac{\dot{Q}_{actual}}{\dot{Q}_{theoretical}}
$$

若采用 Fluent 的 Reports > Volume Integrals，可直接对 Heat Release Rate 在 all_reactive_fluid 上做体积分，输出单位应为 W。若导出单元数据后外部计算，则需确认 q_dot_chem 的单位是 W/m³ 还是 W；若变量已是每单元总热释放，则不能再次乘以单元体积。

> 重要修正：中期报告公式中 Etheoretical 写作 m_dot × LHVCH4。为了物理量严格一致，本文建议在后处理中将该 m_dot 明确解释为燃料甲烷质量流量 m_dot_CH4，而不是氧化剂与燃料的总质量流量。若使用总质量流量，会把氧化剂质量错误地乘入燃料热值，导致 ηc 被系统性低估。

### 4.3 等效混合比 φ(x) 及混合均匀性统计量

等效混合比用于描述燃烧室局部氧化剂/燃料混合状态。由于实际喷注和燃烧过程存在强烈空间非均匀性，φ 不应只计算一个全局入口 O/F，而应在燃烧室高温反应区域内进行体积加权统计，得到均值 φ̄ 和标准差 σφ。

| 采集位置 | Fluent 操作/变量 | 符号 | 说明 |
| --- | --- | --- | --- |
| `s------6.5076` | `temperature` | Ti | 用于筛选高温燃烧统计区 Ti > Tcomb |
| `s------6.5076` | `fmean` | Zi | 当前算例已确认的 Mean Mixture Fraction |
| `s------6.5076` | `o2`、`ch4` 等质量分数 | YO2,i、YCH4,i | 仅作为无混合分数时的备选，不作为当前优先方案 |
| `s------6.5076` | `cell-volume` | Vi | 用于体积加权平均和标准差 |

$$
V_{hot} = \{ i \mid T_i > T_{comb} \}
$$

$$
\phi_i = \frac{Y_{ox,i}/Y_{fuel,i}}{4} \quad \text{(using mass fractions)}
$$

$$
\phi_i = \frac{(1 - Z_i)/Z_i}{4} \quad \text{(using mixture fraction for pure streams)}
$$

$$
\bar{\phi} = \frac{\sum_{i \in V_{hot}} \phi_i V_i}{\sum_{i \in V_{hot}} V_i}
$$

$$
\sigma_\phi = \sqrt{\frac{\sum_{i \in V_{hot}} (\phi_i - \bar{\phi})^2 V_i}{\sum_{i \in V_{hot}} V_i}}
$$

| 量 | 含义 | 后处理要求 |
| --- | --- | --- |
| Tcomb | 燃烧统计区温度阈值 | 建议由基准算例温度场确定后固定；可先进行 1000 K、1200 K、1500 K 敏感性分析 |
| φ̄ | 高温区平均等效混合比 | 用于判断整体偏富燃或富氧；工程优化未必要求等于 1 |
| σφ | 高温区等效混合比标准差 | 用于表征混合均匀性；越小表示高温燃烧区 O/F 空间分布越均匀 |
| 有效样本体积 | ΣVi, i∈Vhot | 用于检查阈值是否过高。若高温区体积过小，φ 统计会被局部热点主导 |

对于非预混燃烧模型，推荐优先基于 mixture fraction 计算 φ，因为混合分数是守恒标量，能更稳定地表征推进剂混合历史。直接使用反应后的 O2 与 CH4 质量分数时，在火焰区内反应物被消耗，可能出现 YCH4 或 YO2 极小导致 φ 数值异常。因此若必须使用物种质量分数，应对极小值设置下限，例如 Yfuel > 1×10⁻⁶，或改用元素混合分数/守恒标量。

### 4.4 推力室壁面平均温度 Twall

壁面平均温度用于反映总体热负荷。中期报告中明确应考察喷注器面、燃烧室侧壁面和喷管侧壁面的平均温度，因此后处理不应只导出一个“全部壁面平均值”。推荐先计算分区平均，再根据研究需要计算全壁面面积加权平均。

| 采集位置 | Fluent 操作/变量 | 符号 | 说明 |
| --- | --- | --- | --- |
| `wall_chamber` | Area-Weighted Average of Wall Temperature | Twall,chamber | 燃烧室主体热负荷 |
| `wall_nozzle` | Area-Weighted Average of Wall Temperature | Twall,nozzle | 喷管热负荷；可反映下游燃气热状态 |
| `wall_throat` | Area-Weighted Average of Wall Temperature | Twall,throat | 喉部平均热负荷 |
| `wall_top`、`wall_gap`、`s------6` | Area-Weighted Average 或外部面积加权 | Twall,other | 全壁面统计的补充区域 |
| 全部壁面合并 | Area、Wall Heat Flux | Aj、q_wall | 用于复核热负荷，特别是固定壁温边界情况下 |

$$
T_{wall,j} = \frac{\int_{A_j} T_w\,dA}{A_j}
$$

$$
T_{wall,total} = \frac{\sum_j T_{wall,j} A_j}{\sum_j A_j}
$$

如果 Fluent 求解采用绝热壁面，Wall Temperature 代表绝热壁温，可用于比较不同喷注器构型造成的燃气侧热环境差异。如果采用固定壁温边界，则 Twall 被边界条件锁定，不能作为优化标签；此时应同时或改用 Wall Heat Flux 的面积平均值和总热流量作为热负荷指标。若采用共轭传热模型，则应明确采集燃气侧壁面温度、固体壁内最高温度或冷却通道侧壁面温度，避免不同物理位置的数据混用。

### 4.5 推力室壁面最大温度 Tmax

Tmax 用于识别局部热结构风险。液体火箭推力室中喉部通常是热流密度和热负荷最严苛的位置之一，因此中期报告中提出优先观察喉部温度峰值。后处理中应输出喉部最大壁温，同时保留全壁面最大壁温用于捕捉喷注面唇口、燃烧室局部回流区或喷管局部异常热点。

| 采集位置 | Fluent 操作/变量 | 符号 | 说明 |
| --- | --- | --- | --- |
| `wall_throat` | Surface Maximum of Wall Temperature | Tmax,throat | 主热风险指标；当前算例已有独立喉部壁面 |
| `wall_chamber` + `wall_nozzle` + `wall_throat` + `wall_top` + `wall_gap` + `s------6` | Surface Maximum of Wall Temperature | Tmax,all | 防止非喉部区域出现更高热点而被遗漏 |
| 全部壁面面元 | Wall Temperature 分布 | P99 或 P99.5 | 作为抗网格噪声的稳健辅助指标 |
| 热点位置 | 面元坐标 x,y,z 与 Tw | hotspot location | 用于判断热点是否稳定、是否由网格畸变或局部数值振荡导致 |

$$
T_{max,throat} = \max(T_w) \quad \text{on throat\_wall}
$$

$$
T_{max,all} = \max(T_w) \quad \text{on all thrust-chamber walls}
$$

若喉部没有独立边界名称，应在几何或网格阶段创建 throat_wall。推荐范围为最小截面积位置前后覆盖喉部圆角和近喉短段，例如 xt ± 0.5Dt 或根据实际喉部圆角几何划分。若直接在全壁面上求最大值，单个异常面元可能对优化结果产生过大影响，因此建议同时输出面积分位数温度，如 P99.5(Tw)，作为识别数值孤点的辅助量。

## 5 自动化实施建议：Fluent / PyFluent 后处理任务拆分

为了服务深度学习数据集构建，后处理流程应写成可重复执行的脚本。本仓库当前已落地两个文件：

- `executor/remote_scripts/metrics_export_gen4.jou`：记录 Fluent 2024 R1 journal 模板、当前真实 zone/field 名称和 report/export 设计。
- `executor/remote_scripts/compute_metrics_gen4.py`：读取 Fluent 导出的 CSV，统一计算五项指标和质量控制量。

当前方案不是让 journal 直接计算五项指标，而是把 journal 限定为“Fluent 数据采集层”，把指标公式放在可单元测试的 Python 脚本中。原因是 Isp 需要面元级动量/压力积分，φ 需要高温区体积加权统计，Twall/Tmax 需要分区和全壁面组合逻辑，这些逻辑在 Python 中更容易测试、维护和复算。

| 模块 | 任务 | 输出 |
| --- | --- | --- |
| Fluent/PyFluent 读取层 | 读取 `model_gen4_<id>.cas.h5` 和 `.dat.h5`，确认 solver server 健康 | 已加载会话 |
| Fluent report 层 | 采集 `mdot_oxidizer`、`mdot_fuel`、`mdot_outlet`、`qdot_actual`、壁面面积/平均温度/最大温度 | `metrics_reports.csv` |
| 出口面导出 | 导出 `outlet` 上的 `density`、`x-velocity`、`pressure`、`x-face-area` | `exit_surface.csv` |
| 反应域导出 | 导出 `s------6.5076` 中的 `temperature`、`fmean`、`cell-volume`、`heat-release-rate` | `chamber_cells.csv` |
| 壁面导出 | 导出所有壁面分区的 `zone`、`temperature`、`face-area-magnitude`、`heat-flux` | `wall_faces.csv` |
| Python 指标计算 | 读取上述 CSV，按统一公式计算五项指标和质量控制量 | `metrics_summary.csv` |

建议实际批处理时优先使用 PyFluent settings API 生成 CSV，因为远程探测表明 Fluent TUI ASCII 导出命令在 journal prompt 顺序上较脆弱；journal 文件保留为可追溯的 Fluent 操作模板和 report 命令记录。PyFluent 或 journal 均应避免硬编码 zone-id，所有面和体区域必须按名称调用；每次后处理前检查所需名称是否存在，不存在则终止并返回错误状态。

## 6 计算结果检查、收敛判据与异常处理

五项指标会作为代理模型训练标签，因此应对每个算例输出质量控制量，避免把未收敛或物理异常的标签混入训练集。建议至少设置以下检查。

| 检查项 | 检查量 | 建议判据 | 异常处理 |
| --- | --- | --- | --- |
| 质量守恒 | eps_m = \|m_dotin - m_dotout\| / m_dotin | < 0.5% 或按课题统一标准 | 超过阈值时 Isp 与 ηc 均可能失真 |
| 热释放合理性 | 0 < ηc < 1.2 | 不应出现明显负值或过大值 | 若 ηc>1，检查热释放率单位和理论热输入质量流量 |
| 出口压力项 | 压力推力占总推力比例 | 与喷管工况相符 | 若异常大，检查 pa、出口边界和法向方向 |
| φ 统计区 | 高温区体积占比、φ 极值 | 高温区不应过小；φ 极值不应由分母接近零主导 | 必要时调整 Tcomb 或采用混合分数 |
| 壁温指标 | Tmax 位置和 P99.5(Tw) | 最大值应有物理连续性 | 单点峰值需结合壁面温度云图判读 |
| 网格相关性 | 基准构型压力、热释放、壁温随网格变化 | 变化趋于收敛 | 网格无关性未通过时不应开展大规模优化 |

对于稳态仿真，应在残差满足要求后继续观察关键物理量是否稳定，例如燃烧室压力、出口质量流量、体积热释放率、壁面平均温度和 Tmax。对于伪瞬态或非稳态仿真，应在时间平均窗口内计算上述指标，避免使用瞬时值作为训练标签。若研究阶段采用第一工况时间平均边界条件，则所有设计构型的边界条件应保持完全一致，只改变喷注器几何分布参数。

## 7 推荐输出数据表结构

| 字段名 | 单位/类型 | 说明 |
| --- | --- | --- |
| case_id | 字符串 | 算例编号，与几何参数文件和 Fluent case/data 文件对应 |
| N1,N2,N3,N4 或其他设计变量 | 整数/浮点 | 喷注器参数化设计变量 |
| mdot_oxidizer, mdot_fuel, mdot_total, mdot_outlet | kg/s | 入口流量、总入口流量和出口流量 |
| F_momentum, F_pressure, F_total | N | 动量推力、压力推力、总推力 |
| Isp | s | 比冲 |
| Qdot_actual, Qdot_theoretical, eta_c | W, W, - | 实际热释放、理论热输入、燃烧效率 |
| phi_mean, phi_std, hot_volume | -, -, m³ | 高温区等效混合比均值、标准差和统计体积 |
| Twall_chamber, Twall_nozzle, Twall_throat, Twall_total | K | 分区和总壁面平均温度 |
| wall_area | m² | 用于 Twall_total 复核的总壁面面积 |
| Tmax_throat, Tmax_all, T_p995_wall | K | 喉部最大壁温、全壁面最大壁温、稳健高分位温度 |
| mass_imbalance, residual_status, convergence_flag | -, 字符串, 0/1 | 质量控制字段 |

## 8 结论

本报告给出的 Fluent 后处理方案，已经从概念性“建议命名”更新为当前 Gen4 算例可执行的“数据采集层 + Python 计算层”方案。五项研究指标分别映射为：Isp 来自 `inlet_oxidizer`、`inlet_fuel` 和 `outlet` 面元推力积分；ηc 来自 `s------6.5076` 的 `heat-release-rate` 体积分和 `inlet_fuel` 甲烷质量流量；φ̄ 与 σφ 来自 `s------6.5076` 高温区 `fmean` 和 `cell-volume` 的体积加权统计；Twall 来自 `wall_chamber`、`wall_nozzle`、`wall_throat` 等壁面分区的面积加权温度；Tmax 来自 `wall_throat` 和全壁面温度最大值。

在自动化仿真体系中，应把 Fluent 计算结果统一转化为 `metrics_summary.csv`，并保留 `metrics_reports.csv`、`exit_surface.csv`、`chamber_cells.csv`、`wall_faces.csv` 等原始导出数据。这样既能满足代理模型训练对结构化标签的需求，也能在后续误差分析、异常算例复查和物理一致性验证中追溯原始流场依据。

## 附录 A：当前单算例后处理步骤

输入：`case_id`、Fluent case/data 文件、`Tcomb`、`pa`、`LHVCH4`。

1. 在 Fluent 2024 R1 solver 模式中读取 `D:\xkz_1020\case\model_gen4_<id>.cas.h5`，Fluent 会自动读取配套 `.dat.h5`。
2. 检查必要 zone：`inlet_oxidizer`、`inlet_fuel`、`outlet`、`s------6.5076`、`wall_chamber`、`wall_nozzle`、`wall_throat`、`wall_top`、`wall_gap`、`s------6`。
3. 通过 Fluent report 获取 `mdot_oxidizer`、`mdot_fuel`、`mdot_outlet`、壁面面积、壁面面积加权温度、壁面最大温度、`qdot_actual`。
4. 导出 `exit_surface.csv`：`density`、`x_velocity`、`pressure`、`x_face_area`。
5. 导出 `chamber_cells.csv`：`temperature`、`fmean`、`cell_volume`，必要时同时保留 `heat_release_rate`。
6. 导出 `wall_faces.csv`：`zone`、`temperature`、`face_area_magnitude`、`heat_flux`。
7. 运行 `compute_metrics_gen4.py`，读取上述 CSV 和 `metrics_reports.csv`。
8. Python 计算入口总流量、出口质量守恒误差、出口动量推力、压力推力、总推力和 Isp。
9. Python 用 `qdot_actual / (mdot_fuel * LHVCH4)` 计算 ηc。
10. Python 筛选 `temperature > Tcomb` 的反应域单元，用 `phi=((1-fmean)/fmean)/4` 和 `cell_volume` 计算 φ̄、σφ 与 `hot_volume`。
11. Python 用壁面面元温度和面积计算 `Twall_total`、`Tmax_throat`、`Tmax_all`、`T_p995_wall`。
12. 输出 `metrics_summary.csv`，并保留中间 CSV 供复核。

## 附录 B：常见错误及修正方式

| 问题 | 可能原因 | 修正方式 |
| --- | --- | --- |
| 入口质量流量符号为负 | Fluent 面法向导致入口 Mass Flow Rate 为负 | 计算 m_dot 时取入口绝对值，并用出口流量做守恒检查 |
| ηc 明显小于合理值 | 误用总推进剂流量计算理论热输入 | 用 m_dot_CH4 而非 m_dot_total 乘以 LHVCH4 |
| ηc 大于 1 很多 | 热释放率单位重复乘体积，或 LHV/HHV、质量流量单位不一致 | 核对 Heat Release Rate 单位；统一 W、kg/s、J/kg |
| φ 出现极大值 | Yfuel 接近 0 或反应物被消耗 | 优先用 mixture fraction；必要时设置分母下限或改用守恒标量 |
| Twall 不随设计变化 | 壁面边界被设置为固定温度 | 改用壁面热流，或采用绝热壁/共轭传热模型后再比较壁温 |
| Tmax 被单个面元控制 | 局部网格畸变或数值尖峰 | 同时输出 P99/P99.5 和热点坐标，并查看温度云图 |
| 推力压力项符号错误 | 出口法向或推力方向定义不一致 | 确认 nozzle_exit 法向 nx 与发动机轴向定义；必要时统一用矢量积分 |

## 参考依据

[1] 谢铠舟. 基于深度学习方法的液氧甲烷发动机喷注器优化设计研究（中期报告）. 北京航空航天大学，2026. 主要依据第 2.1.2 节研究指标、第 2.2 节技术路径、第 3.3.3 节边界条件和求解方法、第 4.1 节仿真模型问题。
