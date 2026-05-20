# HyperMesh MCP 网格划分逻辑说明

本文档说明当前 MCP 的主要划分逻辑，重点对应根目录代码中的 `run_full_meshing_workflow.py`、`hypermesh_mcp_server.py` 和离线弹窗入口 `launch_meshing_workflow_panel.tcl`。

需要注意：代码日志里通常按 `[1/6]` 到 `[6/6]` 打印进度；为了便于理解，本文按概念整理为 Phase 1 到 Phase 4。

## 1. 整体流程

完整自动划分流程由 `run_workflow` 串起来，整体是：

1. Phase 1：几何探针
   - 对当前 HyperMesh 模型里的 solid 做一次轻量临时表面网格探测。
   - 采集每个 solid 的尺寸、面数量、临时网格数量、源面、边界环、齿面候选等信息。

2. Phase 2：分类、重命名、着色
   - 根据 Phase 1 的探针结果，把每个 solid 分类为 `drag_hex`、`spin_hex`、`gear_aware_tetra` 或 `tetra_plain`。
   - 然后在 HyperMesh 中执行 rename 和颜色设置，让用户能直观看到分类结果。

3. Phase 3：网格划分
   - 先划分 `drag_hex`。
   - 再划分 `spin_hex`。
   - drag 或 spin 失败的 solid 会转入 tetra。
   - 最后对 `tetra_plain` 和 `gear_aware_tetra` 做 2D 表面网格、2D 质量修复、3D tetra、3D 质量修复。
   - 对齿轮启用齿面加密时，齿面会使用更小的 tetra 表面尺寸。

4. Phase 4：保存和报告
   - 保存最终 `.hm` 文件。
   - 统计 shell、tet、hex 等单元数量。
   - 生成报告、诊断日志、弹窗提示和可选 JSON 文件。

## 2. Phase 1：探针

Phase 1 的核心是 `generate_geometry_probe_tcl`。它不会正式保留网格，而是对每个 solid 临时生成一批较粗的 2D 表面网格，用这些临时网格来判断几何特征，探测完就删除。

### 2.1 探针的基本方法

每个 solid 的流程大致是：

1. 隔离当前 solid。
2. 获取 solid 的 surfaces。
3. 用较粗的参数做一次临时表面网格。
4. 统计临时网格的节点、三角形、四边形、边界等信息。
5. 判断可能的 drag 源面、spin 截面、齿轮轴、齿面候选。
6. 删除临时网格和临时节点。
7. 输出一行 `MCP_PROBE_SOLID ...` 结果。

探针默认表面网格比较粗，主要是为了低成本获取拓扑和形状信息，不是最终网格。

### 2.2 探针采集的信息

每个 solid 的探针信息主要包括：

| 字段 | 含义 |
| --- | --- |
| `id` | solid ID |
| `exists` | solid 是否存在 |
| `surf_count` | solid 的 surface 数量，简称 `sc` |
| `elem_count` | 临时表面网格单元数量 |
| `node_count` | 临时表面网格节点数量 |
| `tri_count` | 临时三角形数量 |
| `quad_count` | 临时四边形数量 |
| `bbox_ok` | 是否成功获取包围盒 |
| `x0 y0 z0 x1 y1 z1` | solid 包围盒的最小和最大坐标 |
| `dx dy dz` | 三个方向尺寸 |
| `diag` | 包围盒对角线长度 |
| `slender` | 细长程度指标，通常由最大尺寸除以最小尺寸得到 |
| `src_surf` | 推测出来的 drag/spin 源面 ID |
| `drag_axis` | 推测的拉伸方向，通常是最薄的包围盒方向 |
| `gear_axis` | 推测的齿轮轴向 |
| `gear_tooth_surfs` | 识别到的齿面 surface ID 列表 |
| `gear_tooth_count` | 齿面候选 surface 数量 |
| `src_loops` | 源面边界环数量 |
| `src_inner_loops` | 源面内部孔环数量 |
| `src_boundary_edges` | 源面边界边数量 |
| `src_boundary_nodes` | 源面边界节点数量 |

### 2.3 源面和轴向判断

`drag_axis` 通常取包围盒中最薄的方向。比如某个零件在 Z 方向最薄，就倾向认为它可以沿 Z 方向 drag。

`src_surf` 会在候选端面中选择更像源截面的面。判断时会看：

- 该面是否靠近包围盒某一端。
- 该面法向是否接近 drag 方向。
- 该面在另外两个方向上的跨度是否足够覆盖截面。
- 该面边界环数量、孔数量是否适合 drag 或 spin。

`gear_axis` 通常来自两个尺寸接近的方向。齿轮大多有两个较大的径向尺寸和一个较小的厚度方向，因此代码会尝试从包围盒比例中推测齿轮轴。

### 2.4 齿轮和齿面探针

齿轮探针会寻找沿圆周重复出现的面。当前逻辑重点看：

- 是否存在数量足够多的重复面。
- 重复面是否围绕某个轴形成环状分布。
- 重复面之间是否相邻、连续。
- 重复面是否有平面和斜面组合，能形成齿形的曲折轮廓。
- 是否避开外壳、圆筒、带孔支架这类虽然有重复结构但不是齿轮的零件。

当前齿面探针不是只按一种固定齿轮形状处理，而是针对几类已经遇到的齿轮形态分别收敛：

- 薄盘外齿轮：厚度方向明显小于两个径向尺寸，外圈有大量连续重复齿面。齿面一般出现在外半径附近，候选面需要同时满足环向局部、小角度跨度、足够外侧半径等条件。
- 内齿圈：整体是薄环，齿面在内圈，不能只看外半径。代码会结合单个密集内边界环、较高边界节点数和齿面候选数量识别。
- 小太阳轮/小外齿轮：体积较小、源面边界很密，齿面 surface 可能被 HyperMesh 分得比较碎。代码会允许通过单个密集齿形边界和紧凑圆形包围盒补充识别。
- 高面数外齿环：没有稳定 `src_surf`，但 `surf_count` 很高，外圈齿面数量很多，例如大外齿环。代码会在 `surf_count >= 400`、圆形包围盒比例合理、且探针确实找到了足够重复齿面的前提下归入齿轮；齿面候选主要看中心半径、外半径、局部跨度和角度跨度，不再把 bbox 内半径作为硬条件，避免真实外齿因为斜面 bbox 误差被清空。对 s2/s36 这类 `19 x 75 x 75` 薄盘，名义外半径路径允许齿面轴向占比低到约 `0.06`，以覆盖真实齿面 `axis_ratio ~= 0.075` 的情况；另有一条窄轴向齿侧面条件覆盖 `axis_ratio ~= 0.02`、局部跨度很小、外半径接近名义外径的齿面。仍用外圈半径、局部跨度和角度跨度排除内侧孔壁、辐条边界、宽环面和普通盘面。
- 轴上局部齿圈：齿轮长在轴中间，不一定靠近轴端。代码会使用 `shaft_inline_gear` 路径，只在实体细长、横截面接近圆、surface 数量处于窄范围、且探针找到重复齿面时才认为是齿轮。
- 厚/倒角齿轮：齿顶、齿侧、倒角或根部面比较多，单个齿面形态不完全一致。代码会允许较低的齿面密度，但仍要求整体包围盒接近齿轮形态并且重复面成环。

识别结果会写入 `runs/gear_tooth_recognition_<时间戳>.txt`，用于后续排查某个模型为什么识别错。

## 3. Phase 2：分类

Phase 2 的核心是 `classify_all_solids_from_probe`。它只根据 Phase 1 的探针结果做决策，不重新计算几何。

当前主要分类有四类：

| 分类 | 含义 |
| --- | --- |
| `drag_hex` | 适合用源面拖拽生成六面体 |
| `spin_hex` | 适合切截面后旋转生成六面体 |
| `gear_aware_tetra` | 齿轮类零件，使用 tetra，但齿面加密 |
| `tetra_plain` | 普通 tetra 划分 |

### 3.1 分类优先级

当前优先级大致是：

1. 如果启用了齿面加密，并且识别为齿轮，则优先归类为 `gear_aware_tetra`。
2. 不是齿轮时，先判断是否适合 `drag_hex`。
3. drag 不满足时，再判断是否适合 `spin_hex`。
4. 其余全部进入 `tetra_plain`。

这样设计的原因是：drag 和 spin 是六面体路径，要求几何结构比较明确；普通复杂零件走 tetra 更稳。齿轮虽然也可能局部可六面体化，但当前齿轮策略重点是保证齿面加密和稳健 tetra。

### 3.2 drag_hex 分类原理

`drag_hex` 主要识别两类：

- 薄板、块状、源面简单的实体。
- 短圆柱或近似柱状、端面简单的实体。

核心判断信息包括：

- `surf_count` 是否较少。
- 包围盒最小尺寸和最大尺寸比例是否较小，也就是是否明显有厚度方向。
- 是否找到 `src_surf`。
- 源面的内部孔数量是否少。
- 源面边界是否适合做全四边形面网格。

drag 的前提是源面能划出全 quad，否则后续 drag 生成 hex 的质量和完整性都很难保证。

### 3.3 spin_hex 分类原理

`spin_hex` 用于环形、盘形、轴对称特征比较明显的实体。

核心判断信息包括：

- 两个径向尺寸是否接近。
- 厚度方向是否明显小于径向尺寸。
- 是否找到合适源面。
- 源面是否有一个内环，类似环形截面。
- `surf_count` 是否在一个适中的范围内，目前主要控制在大约 `6` 到 `28`。

实心圆盘一般不会走 spin，因为中心附近 spin 出来的网格质量容易很差。

### 3.4 gear_aware_tetra 分类原理

齿轮分类依赖齿面识别结果，而不是只看名字。

主要判断：

- 齿面候选数量是否足够，通常要大于一个最小重复数量。
- 齿面候选是否围绕同一个轴成环。
- 齿面候选是否连续相邻，而不是随机分散的重复结构。
- 齿面中是否同时包含齿顶、齿侧、齿根等曲折特征。
- 结合包围盒比例，排除壳体、罩壳、孔阵列、加强筋等伪齿轮。

当前 `gear_aware_tetra` 主要覆盖以下几类齿轮分类入口：

| 齿轮类型 | 主要触发条件 | 保护条件 |
| --- | --- | --- |
| 薄盘外齿轮 | `surf_count` 较高，两个径向尺寸接近，厚度相对较小，齿面候选密度高 | 要求齿面候选数量和密度都达到阈值，避免孔阵列或加强筋误判 |
| 内齿圈 | 单个密集内齿边界、边界节点很多，外形是薄环 | 需要单个内环和足够齿面候选，避免普通带孔圆盘误判 |
| 小太阳轮/小外齿轮 | 紧凑圆形实体，源面边界密集，齿数较多 | 限定 `surf_count`、厚径比、内环数量和边界节点数 |
| 高面数外齿环 | 无稳定源面，`surf_count >= 400`，圆形外包络明显，探针找到重复齿面；名义外半径路径覆盖 `axis_ratio` 约 `0.06` 以上的齿面，并用窄轴向齿侧面条件补齐 `axis_ratio` 约 `0.02` 的齿面 | 候选面中心半径和外半径要靠近外圈，并限制局部跨度、角度跨度和径向跨度，避免复杂圆壳、内侧孔壁、宽环面或辐条边界误判 |
| 轴上局部齿圈 | 实体沿轴向较长，横截面接近圆，齿圈可能位于轴中间 | 必须探测到重复齿面；普通圆轴因为 `gear_tooth_count=0` 不会进齿轮 |
| 厚齿轮/倒角齿轮 | 外形较厚或带倒角，齿面候选密度低于薄盘齿轮 | 需要紧凑包围盒和足够重复齿面，避免大型壳体、支架误判 |

如果启用了 `use_gear_tooth_refinement`，识别为齿轮后不会归入普通 tetra，而是归入 `gear_aware_tetra`。

如果未启用齿面加密开关，齿轮会按普通 tetra 处理，尽量保持旧版本行为。

### 3.5 tetra_plain 分类原理

不能稳定 drag、spin 或 gear-aware 的实体都会进入 `tetra_plain`。

典型情况包括：

- 面数量很多。
- 结构复杂。
- 源面不明确。
- 有多个孔环。
- 包围盒比例不适合 drag 或 spin。
- 齿轮识别关闭或齿面重复特征不足。

tetra 路径虽然网格数量可能更多，但对复杂几何更通用。

## 4. Phase 3：划分

Phase 3 是实际生成网格的阶段。顺序通常是：

1. `drag_hex`
2. `spin_hex`
3. `tetra_plain` 和 `gear_aware_tetra`

drag 或 spin 失败的实体会回退到 tetra。

### 4.0 尺寸计算公式总览

下面的公式是当前代码的实际口径。为了便于阅读，先定义：

```text
clamp(x, lo, hi) = max(lo, min(x, hi))
```

#### drag_hex 尺寸和层数

设：

- `D` = drag 距离，也就是实体沿拖拽方向的厚度。
- `source_minor` = 源面包围盒三个尺寸排序后的中间值。
- `source_major` = 源面包围盒最大值。
- `requested_size` = 用户或 agent 请求的 drag 尺寸。
- `drag_min`、`drag_max` = drag 尺寸上下限，当前常用范围约 `0.5..1.5`。

初始 drag 尺寸：

```text
thickness_size = D / 4
source_size = min(source_minor / 3, source_major / 8)
drag_size = clamp(min(requested_size, thickness_size, source_size), drag_min, drag_max)
```

drag 层数：

```text
drag_layers = max(1, round(D / drag_size))
```

如果启用了“drag 三层”：

```text
drag_layers = max(drag_layers, 3)
```

生成六面体后会检查 hex aspect。若 aspect 超过阈值，代码会减小尺寸重试，最多 3 次；仍失败时回退到 1 层 drag，并在报告中标记。

#### spin_hex 截面尺寸和旋转份数

设：

- `section_minor` = 截面候选面的包围盒三个尺寸排序后的中间值。
- `section_major` = 截面候选面的包围盒最大值。
- `requested_section_size` = 请求的 spin 截面 2D 尺寸。
- `section_min`、`section_max` = spin 截面尺寸上下限。

截面基础尺寸：

```text
section_size = clamp(
  min(requested_section_size, section_minor / 3, section_major / 10),
  section_min,
  section_max
)
```

截面最少 shell 数估计：

```text
area_est = section_minor * section_major
min_shells = clamp(ceil(area_est / (section_size * section_size * 4)), 2, 80)
```

旋转份数按半径自适应：

```text
raw_density = ceil(2 * pi * max_radius / section_size)
spin_density = clamp(raw_density, spin_density_min, spin_density_max)
```

其中 `max_radius` 来自截面 shell 节点到旋转轴的最大距离。

#### tetra_plain 表面尺寸

设：

- `element_size` = 用户或 agent 请求的 tetra 表面目标尺寸。
- `element_size_min`、`element_size_max` = tetra 目标尺寸上下限。
- `min_element_size` = 请求的最小尺寸。
- `min_element_size_min`、`min_element_size_max` = 最小尺寸上下限。

普通 tetra 的基础尺寸：

```text
tetra_size = clamp(element_size, element_size_min, element_size_max)
tetra_min_size = clamp(min_element_size, min_element_size_min, min_element_size_max)
```

代码还会根据实体自身尺寸和 surface 数量自动收紧尺寸：

```text
auto_tetra_size = clamp(mid_dim / 4, element_size_min, element_size_max)
tetra_size = clamp(min(element_size, auto_tetra_size), element_size_min, element_size_max)

complexity_min = min_element_size_max
  - min(min_element_size_max - min_element_size_min,
        max(0, (surface_count - 20) / 100 * (min_element_size_max - min_element_size_min)))

dim_min = clamp(min_dim / 8, min_element_size_min, min_element_size_max)

tetra_min_size = clamp(
  min(min_element_size, complexity_min, dim_min),
  min_element_size_min,
  min_element_size_max
)
```

其中 `min_dim`、`mid_dim` 是 solid 包围盒三个尺寸排序后的最小值和中间值。这些限制的目的不是追求更细，而是避免复杂实体一次生成过多 shell 后把 HyperMesh 卡死。

#### gear_aware_tetra 齿面尺寸

齿面默认比普通 tetra 小 30%，即使用比例：

```text
gear_tooth_scale = 0.70
```

默认齿面目标尺寸：

```text
default_tooth_size = tetra_size * 0.70
```

默认齿面最小尺寸：

```text
default_tooth_min_size = tetra_min_size * 0.70
```

默认齿面特征角：

```text
default_tooth_feature_angle = feature_angle * 0.70
```

如果离线弹窗或 agent 参数传入齿面尺寸范围，则实际使用：

```text
tooth_size = clamp(requested_tooth_size, tooth_size_min, tooth_size_max)
tooth_min_size = clamp(requested_tooth_min_size, tooth_min_size_min, tooth_min_size_max)
```

当上下限设成同一个值时，`clamp` 后就是固定值。

#### 贴合和防崩阈值

tetra 2D shell 的 bbox 贴合容差当前按以下形式估算：

```text
fit_tol = max(tetra_size * 0.25, solid_diag * fit_tolerance_ratio)
```

其中 `solid_diag` 是 solid 包围盒对角线长度。

当前主要防崩阈值：

```text
fatal_surface_aspect = 2000
surface_chord_deviation = 0.1
tetra_crash_guard_shells = 150000
```

如果修复后的 2D shell 超过防崩阈值、存在极端 aspect、贴合或 chord dev 明显变差，代码会停止进入 tetra，保留当前 2D，并在最终报告中说明。

### 4.1 drag_hex 划分

drag 的核心流程：

1. 使用 Phase 1 找到的 `src_surf`。
2. 在源面上划分 2D 网格。
3. 要求源面网格是全 quad。
4. 根据厚度、源面大小、用户尺寸上下限计算 drag 尺寸。
5. 计算 drag 层数。
6. 使用 HyperMesh drag 命令生成六面体。
7. 检查是否生成了 hex，以及生成后的尺寸和贴合是否合理。

drag 的尺寸大致由三类因素共同决定：

- 用户给定的 drag 尺寸范围。
- solid 的厚度方向尺寸。
- 源面的长宽尺寸。

代码会把尺寸夹在离线弹窗或 agent 参数给定的上下限内。

如果启用了“drag 三层”逻辑：

- drag 最少尝试 3 层。
- 生成后检查六面体 aspect。
- aspect 小于阈值则通过。
- aspect 不合格会减小尺寸重试，最多 3 次。
- 如果仍然失败，会退回一层 drag，并在最终报告中说明。

agent 版本默认使用 drag 三层逻辑；离线版本弹窗中有对应开关，当前默认勾选。

### 4.2 spin_hex 划分

spin 的核心流程：

1. 通过旋转轴切一个横截面。
2. 切分后会得到两个截面，代码从中选一个真实截面。
3. 在截面上划分 2D 网格。
4. 将截面网格绕轴旋转生成 3D 六面体。
5. 如果失败，回退到 tetra。

当前唯一支持的 spin 路径就是 cut-section spin：切出新截面、在真实截面上划 2D、再 spin 成体。代码不再使用旧的直接源面 spin 路径，也不会把已有预切分表面当作截面兜底。

spin 截面尺寸由截面大小和用户给定上下限共同决定：

- 离线弹窗有 spin 截面 2D 网格尺寸上下限。
- 默认下限约 `0.20`，上限约 `1.50`。

spin 旋转份数按半径智能判断：

- 先根据最大旋转半径估算周长。
- 用周长除以截面目标尺寸得到建议份数。
- 再夹在 `spin_density_min` 和 `spin_density_max` 之间。
- 离线弹窗中可以设置旋转份数上下限。

如果 spin 失败或生成结果不满足要求，代码会把该 solid 重新纳入 tetra 队列。为了避免之前出现的“solid 被切成两半后只划一半”的问题，失败回退时会尽量恢复或合并到可继续 tetra 的状态。

### 4.3 tetra_plain 划分

普通 tetra 的流程分成 2D 和 3D 两大段：

1. 对 solid 的所有 surface 划分 2D shell。
2. 做 2D 质量检测。
3. 对不合格的 2D shell 做修复。
4. 修复后再次检测贴合和 2D 质量。
5. 如果通过防崩和质量门槛，则生成 tetra。
6. 检测 3D tetra 质量。
7. 对 3D 质量做修复。
8. 如果 3D 仍不合格，则删除 tetra，保留修复后的 2D shell。

tetra 的默认表面网格参数来自离线弹窗或 CLI：

- 目标尺寸范围默认约 `1.5` 到 `2.0`。
- 最小尺寸范围默认约 `0.20` 到 `0.50`。
- surface deviation 默认约 `0.05`。
- feature angle 默认约 `15`。
- growth rate 默认约 `1.23`。

对于复杂实体，会按 surface 数量降低最小尺寸并降低允许进入 tetra 的 shell 数量，减少 HyperMesh 卡死或崩溃风险。

### 4.4 gear_aware_tetra 划分

齿轮当前仍走 tetra 体系，但齿面会加密。

如果启用了齿面加密：

1. Phase 1 识别齿面 surface。
2. Phase 2 把实体分类为 `gear_aware_tetra`。
3. Phase 3 对齿面和非齿面使用不同 2D 网格尺寸。
4. 齿面尺寸默认比普通 tetra 小约 30%。
5. 离线弹窗中可以单独设置：
   - 齿面 tetra 尺寸范围。
   - 齿面 tetra 最小尺寸范围。
   - 齿面 feature angle。

当齿面尺寸上下限设为同一个值时，实际执行就是固定这个值；最小尺寸范围同理。

此外还有“只划分齿面网格”的调试功能：

- 执行 rename 和着色。
- 只对识别到的齿面生成预览网格。
- 不做完整 tetra。
- 用于验证齿轮和齿面识别是否正确。

对应还有删除齿面预览网格的功能，用于看完后清理预览结果。

### 4.5 贴合度检测

当前贴合度不是完整的几何投影距离，而是两类近似检测组合：

1. bbox 贴合检测
   - 比较修复前后 shell 的包围盒和 solid 包围盒差异。
   - 如果修复后 bbox 偏差明显变大，认为贴合度下降。

2. chordal deviation 检测
   - 使用 HyperMesh 原生命令检测 chordal deviation。
   - 当前阈值约 `0.1`。
   - 修复后如果 chordal deviation 不合格数量明显增加，也认为贴合风险上升。

如果 2D 修复后贴合度明显下降：

- 如果还有重试机会，会减小或调整 2D 表面网格参数重新划分。
- 如果已经没有下一轮，会再做一轮更保守的修复。
- 如果仍不合格，则不进入 tetra，保留当前 2D，并在报告中提示。

### 4.6 2D 质量检测和修复

2D 质量检测主要依赖 HyperMesh 原生命令，而不是自己用坐标随便计算。

aspect 检测使用类似：

```tcl
*createmark elements 1 "displayed"
*createmark elements 2
*elementtestaspect elements 1 10 2 2 0 "  2D Aspect Ratio  "
```

如果原生命令失败，代码才会使用几何坐标算法作为 fallback。

关键阈值：

- 常规 2D aspect 阈值：`10`。
- 极端 aspect 防崩阈值：当前约 `2000`。
- shell 数量防崩阈值：当前约 `150000`。
- chordal deviation 阈值：当前约 `0.1`。

2D 修复策略大致包括：

- triangle cleanup。
- smooth。
- local remesh。
- replace nodes。

正常情况下先执行一轮修复。如果仍有不合格 2D，会再执行一轮 replace 类修复。

针对疑似破面、叠网或高风险模型，代码会更保守：

- 跳过容易卡死的修复方式。
- 每个修复方式之间检查超时。
- 如果修复阶段超过约 5 分钟，会停止当前实体的危险修复路径，避免 HyperMesh 长时间无响应。
- 对高风险实体尽量保留修复后的 2D，不强行进入 tetra。

### 4.7 3D tetra 质量检测和回退

tetra 生成前，代码会先复制一份修复后的 2D shell 到临时 backup component。

这样做的目的：

- tetmesh 失败时，可以删除 tetra 并恢复修复后的 2D。
- 避免 3D 阶段修改或破坏已经修好的 2D shell。

tetmesh 阶段会尽量使用不更新 shell 的参数，避免生成 3D 时改写输入 2D。

3D 质量主要看 vol skew：

- 生成目标 vol skew 默认约 `0.70`。
- 修复阈值默认约 `0.99`。

如果 3D 修复后仍不合格：

- 删除 tetra。
- 恢复或保留修复后的 2D shell。
- 在最终弹窗和报告里说明 3D 修复前后不合格数量，以及保留下来的 2D 状态。

## 5. Phase 4：保存

Phase 4 会保存最终模型，并输出统计和报告。

主要内容：

1. 保存 `.hm` 文件到输出路径。
2. 统计最终模型中的：
   - 总 element 数量。
   - shell 数量。
   - tet4 数量。
   - tet10 数量。
   - hex8 数量。
   - 其他单元数量。
3. 解析 2D 和 3D 修复摘要。
4. 输出最终中文报告。
5. 在 HyperMesh 中弹窗提醒需要用户关注的实体。

如果有实体因为 2D 防崩、极端 aspect、贴合下降、3D 修复失败等原因没有进入最终 tetra，会在最终报告中分类列出。

## 6. 其他机制

### 6.1 离线版本和 agent 版本的关系

离线弹窗和 agent 自动执行本质上共用同一套 Python 和 Tcl 生成逻辑。

区别主要是：

- 离线版本由 `launch_meshing_workflow_panel.tcl` 提供输入框和复选框。
- agent 版本由命令行参数传入。
- 真正的 probe、classify、mesh、save 逻辑都在根目录 Python 代码中。

因此，分类、齿面识别、drag、spin、tetra、修复和报告逻辑应同时影响离线版本和 agent 版本。

### 6.2 诊断文件

为了排查闪退、卡死、识别错误和划分失败，当前流程会生成多种文件：

| 文件 | 用途 |
| --- | --- |
| `runs/workflow_diagnostics_<时间戳>.jsonl` | 实时诊断日志，可快速定位卡在哪一步、Tcl 是否报错、是否疑似闪退 |
| `runs/workflow_probe_<时间戳>.txt` | Phase 1 原始探针结果 |
| `runs/gear_tooth_recognition_<时间戳>.txt` | 齿轮和齿面识别过程、识别原因、齿面 ID |
| `runs/workflow_tetra_batch_<编号>_<时间戳>.log` | tetra 批处理日志 |
| `runs/workflow_report_<时间戳>.txt` | 最终中文报告 |
| 可选 JSON 报告 | 供程序或 agent 进一步读取 |

后续如果某次 HyperMesh 关闭或卡住，优先看 `workflow_diagnostics`，再看对应批次日志和齿轮识别日志。

### 6.3 已有网格跳过

流程会检测某些 solid 是否已经有网格。如果已有网格，可能会跳过重新划分，避免重复生成或破坏现有结果。

### 6.4 tetra 分批

tetra 不会把所有实体一次性丢给 HyperMesh。

分批原则大致是：

- 高风险实体单独执行。
- 面多、预计 shell 多、复杂度高的实体减少同批数量。
- 普通实体可以合批执行。

这样可以降低一次 Tcl 执行时间过长、HyperMesh 界面白屏或闪退的概率。

### 6.5 退回 2D 的几类原因

最终保留 2D 网格的常见原因包括：

- shell 数量超过防崩阈值，不进入 tetra。
- 2D aspect 存在极端值，不进入 tetra。
- 2D 修复后贴合度下降，不进入 tetra。
- 3D tetra 生成成功但质量修复后仍不合格，删除 tetra 并恢复 2D。
- 修复过程疑似卡死或超时，停止当前危险路径。

这些信息会在最终弹窗和报告里尽量分组显示，便于用户区分是 2D 风险、3D 风险，还是几何本身异常。
