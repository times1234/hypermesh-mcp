# HyperMesh 离线划分流程 SOP

这份 SOP 用来在不使用 AI agent 的情况下运行完整网格划分流程。

离线版本仍然复用当前 MCP 中的划分逻辑，但流程调度由
`run_full_meshing_workflow.py` 这个普通 Python 脚本完成。

> 建议用支持 UTF-8 的编辑器查看本文件。某些 PowerShell 默认编码下直接
> `Get-Content` 可能显示乱码，但不影响脚本运行。

## 0. 运行前必要配置

运行离线版本前，需要满足以下条件：

1. **Windows 环境**

   当前脚本和命令按 Windows PowerShell 编写。

2. **已安装 Python**

   建议使用 Python 3.10 或更高版本。

   在 PowerShell 中检查：

   ```powershell
   python --version
   ```

   如果能输出类似 `Python 3.10.x`、`Python 3.11.x` 或 `Python 3.12.x`，
   说明 Python 可用。

3. **已安装 Python 依赖**

   在项目目录中运行：

   ```powershell
   cd E:\mcp\hypermesh-mcp-server
   python -m pip install -r requirements.txt
   ```

   如果电脑没有网络，请使用本文后面的“无网络电脑部署方法”。

   当前 `requirements.txt` 中主要依赖是：

   ```text
   mcp
   ```

4. **已安装 Altair HyperMesh**

   离线流程不是独立网格引擎，它仍然需要调用已经打开的 HyperMesh。

5. **HyperMesh 中已经导入模型**

   运行离线脚本前，需要先在 HyperMesh 中打开或导入待划分模型。

6. **HyperMesh GUI Listener 已建立**

   离线脚本通过 GUI listener 和 HyperMesh 通信，所以必须先完成下面
   “生成 GUI Listener Tcl”和“在 HyperMesh 中 source 这个 Tcl”两步。

7. **端口没有被占用**

   默认 listener 端口是 `47881`。如果端口被占用，可以在生成 listener
   和运行离线脚本时指定其他端口。

8. **不要删除运行目录**

   `runs` 和 `outputs` 会在运行中自动写入日志、Tcl 和结果模型。

## 0.1 无网络电脑部署方法

如果要把整个流程复制到另一台没有网络的电脑上，推荐按下面方式部署。

### 在当前有网络的电脑上准备离线依赖

在项目目录运行：

```powershell
cd E:\mcp\hypermesh-mcp-server
mkdir wheels
python -m pip download -r requirements.txt -d wheels
```

运行完成后，项目目录中会多出一个 `wheels` 文件夹，里面是离线安装用的
Python 依赖包。

### 复制到无网络电脑

把整个文件夹复制到无网络电脑，例如仍然放到：

```text
E:\mcp\hypermesh-mcp-server
```

至少需要包含这些内容：

```text
hypermesh_mcp_server.py
run_full_meshing_workflow.py
requirements.txt
wheels\
OFFLINE_WORKFLOW_SOP.md
README.md
codex_mcp_config.example.json
```

`runs` 和 `outputs` 不是源码，复制时可以带上，也可以不带。没有这两个文件夹时，
脚本运行过程中会重新生成。

### 在无网络电脑上安装依赖（无网络电脑的python版本与有网络电脑一样则该步骤可跳过）

先确认无网络电脑上已经安装 Python。建议目标电脑使用和当前电脑一致的
Python 大版本和架构，当前文件夹内已经包含了依赖。

当前 `wheels` 中包含 Windows 64 位、CPython 3.12 对应的依赖包，因此目标电脑
推荐安装：

```text
Python 3.12 64-bit
```

在无网络电脑 PowerShell 中运行：

```powershell
cd E:\mcp\hypermesh-mcp-server
python -m pip install --no-index --find-links wheels -r requirements.txt
```

安装完成后检查：

```powershell
python -c "import mcp; print('mcp OK')"
```

如果输出：

```text
mcp OK
```

说明离线依赖安装成功。

### 无网络电脑上仍然需要 HyperMesh

无网络部署只解决 Python 依赖问题。目标电脑仍然必须安装并能打开
Altair HyperMesh，因为实际网格划分仍然由 HyperMesh 执行。

### 复制到新电脑后路径变化怎么办

如果新电脑上的项目路径不是：

```text
E:\mcp\hypermesh-mcp-server
```

只需要把本文命令中的 `cd E:\mcp\hypermesh-mcp-server` 改成新电脑上的实际路径。

例如复制到了：

```text
D:\tools\hypermesh-mcp-server
```

则 PowerShell 中使用：

```powershell
cd D:\tools\hypermesh-mcp-server
```

离线版本不要求在代码里写 HyperMesh 安装路径。它连接的是你已经打开并
source listener 的 HyperMesh，所以只要 HyperMesh 能正常打开，并且 listener
source 成功即可。

只有在使用 AI 版 MCP 的启动脚本、批处理模式，或者让脚本自动启动 HyperMesh
时，才需要关心 HyperMesh 的 `hmbatch.exe` / `hw.exe` 路径。

## 1. HyperMesh 弹窗面板运行方式

这是现在推荐的离线运行方式：不用在 PowerShell 里手动跑完整命令，
直接在 HyperMesh 中 source 一个启动面板 Tcl。

### 1.1 打开 HyperMesh 并导入模型

先手动打开 HyperMesh，然后导入需要划分网格的模型。

### 1.2 在 HyperMesh 中打开启动面板

在 HyperMesh 的 Tcl 命令窗口中运行：

```tcl
source "E:/mcp/hypermesh-mcp-server/launch_meshing_workflow_panel.tcl"
```

如果项目复制到了其他路径，把上面的路径改成实际路径即可。例如：

```tcl
source "D:/tools/hypermesh-mcp-server/launch_meshing_workflow_panel.tcl"
```

运行后会出现“HyperMesh 自动网格划分面板”。

也可以通过运行脚本的方式运行

### 1.3 面板里可以修改什么

面板中可以直接修改这些常用参数：

- `drag 尺寸下限 / drag 尺寸上限`：控制 drag 自动尺寸计算后的允许范围，默认是 `0.5..1.5`。
- `drag 贴合比例`：六面体贴合度检查比例。
- `drag 重试次数`：六面体失败后的重试次数。
- `tetra目标下限 / tetra目标上限`：控制 tetra 目标尺寸自动计算后的允许范围，默认是 `1.5..2.0`。
- `tetra最小下限 / tetra最小上限`：控制 tetra 最小尺寸自动计算后的允许范围，默认是 `0.20..0.50`。
- `tetra 最大偏差`：四面体面网格贴合几何的最大偏差。
- `tetra 特征角`：四面体面网格保留特征的角度。
- `tetra 增长率`：四面体网格尺寸过渡速度。
- `tetra 贴合比例`：四面体贴合度检查比例。
- `目标 vol skew`：四面体生成时使用的体单元质量目标。
- `修复 vol skew`：四面体质量修复后接受的上限。

这几项不是固定最终网格尺寸，而是自动尺寸公式的上下限。实际尺寸仍然会根据
实体尺寸、厚度、面数量等信息自动计算。

### 1.4 点击运行

默认勾选“开始前自动建立/刷新 HyperMesh 连接”。一般情况下直接点击：

```text
开始划分
```

即可自动完成：

```text
建立 listener 连接 -> 探测模型 -> 分类 -> drag 六面体 -> tetra 四面体 -> 质量修复 -> 最终保存 -> 中文 txt 报告
```

如果自动建立连接失败，可以先点击“仅建立连接”测试；仍失败时，按本文后面的
命令行方式手动生成并 source listener，然后回到面板，取消勾选
“开始前自动建立/刷新 HyperMesh 连接”，再点击“开始划分”。

点击“开始划分”后不会再弹出额外提示框。当前状态会显示在面板状态栏，
命令行版本原本输出的 `[1/6]`、`[2/6]`、tetra batch、最终单元统计、
报告路径等内容会实时显示在面板下方的“运行日志”区域。

### 1.5 停止当前流程

点击：

```text
停止当前流程
```

会终止由面板启动的后台 Python 流程，并阻止后续批次继续提交。

注意：如果 HyperMesh 已经接收到一段正在执行的 Tcl，停止 Python 后，
HyperMesh 可能会把当前 Tcl 命令执行完才停下来。这是 HyperMesh 执行机制导致的，
不是面板没有响应。

### 1.6 查看结果

面板运行时会在 `runs` 文件夹中写入一个日志文件：

```text
runs/panel_workflow_时间戳.log
```

流程结束后会生成中文报告：

```text
runs/workflow_report_时间戳.txt
```

最终 `.hm` 文件默认保存在面板中填写的输出路径。

## 2. 命令行运行方式

下面是旧的命令行运行方式，适合需要排查连接问题或不使用面板时使用。

## 2.1 打开 HyperMesh 并导入模型

手动打开 HyperMesh，然后导入需要划分网格的模型。

## 2.2 生成 GUI Listener Tcl

打开 PowerShell，运行：

```powershell
cd E:\mcp\hypermesh-mcp-server
python -c "import hypermesh_mcp_server as hm; print(hm.create_gui_listener_tcl()['script_path'])"
```

命令会输出一个 Tcl 文件路径，例如：

```text
E:\mcp\hypermesh-mcp-server\runs\hypermesh_mcp_20260511_xxxxxx_xxxxx.tcl
```

如果需要指定端口，例如 `47882`，可以运行：

```powershell
python -c "import hypermesh_mcp_server as hm; print(hm.create_gui_listener_tcl(port=47882)['script_path'])"
```

## 2.3 在 HyperMesh 中 source 这个 Tcl

回到 HyperMesh 的 Tcl 命令窗口，运行 `source` 命令。

注意：路径要使用第 2 步实际输出的路径，建议把反斜杠 `\` 改成正斜杠
`/`。

示例：

```tcl
source "E:/mcp/hypermesh-mcp-server/runs/hypermesh_mcp_20260511_xxxxxx_xxxxx.tcl"
```

如果成功，HyperMesh 命令窗口里会出现类似提示：

```text
MCP HyperMesh GUI listener is ready on 127.0.0.1:47881
```

## 2.4 测试连接

回到 PowerShell，运行：

```powershell
python -c "import hypermesh_mcp_server as hm; print(hm.execute_tcl_gui('puts {PING_OK}', enforce_meshing_rules=False))"
```

如果返回内容里包含：

```text
PING_OK
```

说明 PowerShell 和 HyperMesh GUI 已经连通。

## 2.5 运行离线划分流程

在 PowerShell 中运行：

```powershell
python run_full_meshing_workflow.py --output outputs\full_mesh.hm
```

如果 listener 使用的不是默认端口，需要显式指定端口。比如 HyperMesh 中显示
端口是 `47881`，则运行：

```powershell
python run_full_meshing_workflow.py --port 47881 --output outputs\full_mesh.hm
```

脚本会自动执行以下流程：

1. 探测当前 HyperMesh 模型中的所有实体。
2. 根据几何信息对实体分类。
3. 执行 Phase 2：组件重命名和颜色设置。
4. 对适合 drag 的实体生成六面体网格。
5. 对需要 tetra 的实体按批次生成四面体网格。
6. 全部划分完成后只保存一次模型。
7. 在 `runs` 文件夹中生成一个中文 TXT 总报告。

默认情况下，离线流程不会再输出一堆 `workflow_*.json` 报告，只输出中文
TXT 总报告。报告文件名类似：

```text
runs\workflow_report_20260511_120000.txt
```

如果需要调试详细过程，可以额外加 `--write-json`，这样才会输出 JSON：

```powershell
python run_full_meshing_workflow.py --write-json --output outputs\full_mesh.hm
```

## 3. 网格尺寸和质量检测参数在哪里改源码

本节说明如果要直接改代码中的数字，应该去哪个文件、哪个位置修改。

下面行号对应当前版本。如果后续代码增删导致行号变化，可以在编辑器里搜索对应
参数名。

### 6.1 tetra 面网格基础参数

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
generate_batched_plain_tetra_tcl(...)
约第 2414-2421 行
```

当前参数：

```python
default_element_size: float = 1.5
default_min_element_size: float = 0.5
max_deviation: float = 0.05
feature_angle: float = 15
growth_rate: float = 1.23
fit_tolerance_ratio: float = 0.01
target_vol_skew: float = 0.70
repair_vol_skew: float = 0.99
```

含义：

- `default_element_size`：tetra 面网格基础目标尺寸。
- `default_min_element_size`：tetra 面网格默认最小尺寸。
- `max_deviation`：surface deviation 面网格允许的最大偏差。
- `feature_angle`：特征角。
- `growth_rate`：面网格尺寸增长率。
- `fit_tolerance_ratio`：面网格 bbox 和实体 bbox 的贴合容差比例。
- `target_vol_skew`：tetra 生成阶段的体网格 skew 目标。
- `repair_vol_skew`：体网格质量检查和修复后的 skew 阈值。

常见修改：

```python
default_element_size: float = 1.2
```

会让 tetra 面网格整体更细。

```python
default_element_size: float = 1.8
```

会让 tetra 面网格整体更粗。

### 6.2 tetra 单实体生成函数中的同一组参数

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
generate_plain_tetra_tcl(...)
约第 1953-1962 行
```

当前参数：

```python
min_element_size: float = 0.5
max_deviation: float = 0.05
feature_angle: float = 15
growth_rate: float = 1.23
fit_tolerance_ratio: float = 0.01
target_vol_skew: float = 0.70
repair_vol_skew: float = 0.99
```

说明：

离线完整流程主要走 `generate_batched_plain_tetra_tcl(...)`，但单独划分某一个
tetra 实体时会走 `generate_plain_tetra_tcl(...)`。如果希望 AI 版和单独测试
也保持一致，这两处最好同步修改。

### 6.3 tetra 面网格尺寸上下限和自动收缩规则

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 1983-1984 行
```

当前逻辑：

```python
clamped_min = max(0.20, min(float(min_element_size), 0.50))
clamped_size = max(1.5, min(float(element_size), 2.0))
```

含义：

- `clamped_min` 把最小尺寸限制在 `0.20..0.50`。
- `clamped_size` 把目标尺寸限制在 `1.5..2.0`。

如果你想允许 tetra 面网格更细，可以改这里的 `1.5` 下限，例如：

```python
clamped_size = max(1.0, min(float(element_size), 2.0))
```

如果你想允许更粗，可以改 `2.0` 上限。

另一个关键位置：

```text
约第 2196-2200 行
```

当前逻辑：

```tcl
set auto_elem_size [expr {min(2.0, max(1.5, $mid_dim/4.0))}]
set elem_size [expr {min(2.0, max(1.5, min($requested_elem_size, $auto_elem_size)))}]
set complexity_min [expr {0.50 - min(0.30, max(0.0, ($surf_count - 20) / 100.0 * 0.30))}]
set dim_min [expr {max(0.20, min(0.50, $min_dim/8.0))}]
set base_min_size [expr {max(0.20, min(0.50, min($requested_min_size, min($complexity_min, $dim_min))))}]
```

这里控制根据实体尺寸、面数量自动计算面网格尺寸和最小尺寸。

### 6.4 tetra 每次重试时的尺寸变化

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2206-2212 行
```

当前逻辑：

```tcl
set cs [expr {max(1.5, $elem_size * pow(0.90, $at))}]
set mn_size [expr {max(0.20, $base_min_size * pow(0.80, $at))}]
set effective_growth [expr {min($growth, $surface_growth_limit)}]
set max_size [expr {max($cs * 1.35, $mn_size + 0.05)}]
set max_size [expr {min($max_size, max($mn_size + 0.05, $mn_size * $surface_max_to_min_ratio))}]
set max_size [expr {max($max_size, $cs)}]
```

含义：

- `0.90`：每次 surface mesh 重试时，目标尺寸缩小到上一次的 90%。
- `0.80`：每次重试时，最小尺寸缩小到上一次的 80%。
- `1.35`：最大尺寸和目标尺寸的关系。
- `0.05`：最大尺寸和最小尺寸之间的最小间隔。

如果你不希望重试时尺寸变化太大，可以把 `0.90` 调高，例如 `0.95`。

### 6.5 tetra 面网格质量检测阈值

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2002-2005 行
```

当前参数：

```tcl
set surface_aspect_threshold 10.0
set surface_growth_limit 1.23
set surface_max_to_min_ratio 6.0
set retry_count 4
```

含义：

- `surface_aspect_threshold`：2D 三角形 aspect 超过多少算坏单元。
- `surface_growth_limit`：面网格增长率上限。
- `surface_max_to_min_ratio`：面网格最大尺寸和最小尺寸允许比例。
- `retry_count`：surface mesh 最多尝试次数。

如果你想更严格检查细长三角形，可以把：

```tcl
set surface_aspect_threshold 10.0
```

改小，例如：

```tcl
set surface_aspect_threshold 8.0
```

### 6.6 tetra 面网格贴合度检测

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2253-2267 行
```

当前核心逻辑：

```tcl
set shell_bb [hm_getboundingbox elems 2 0 0 0]
set fit_tol [expr {max($cs * 0.25, $solid_diag * $fit_tol_ratio)}]
...
if {$fit_diff > $fit_tol} {set fit_ok 0}
```

含义：

- `$cs * 0.25`：按当前网格尺寸给一个基础贴合容差。
- `$solid_diag * $fit_tol_ratio`：按实体对角线给一个比例容差。
- 两者取较大值作为最终贴合容差。

如果贴合检查过严，可以小幅调大 `fit_tolerance_ratio`，位置见第 6.1。

### 6.7 2D 三角形修复流程和修复参数

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2273-2297 行
```

当前顺序：

```text
1. triangle_cleanup
2. smooth_5
3. local_remesh
4. replace_nodes
```

关键参数：

```tcl
catch {*triangle_clean_up elems 1 "aspect=6.0 height=0.3"}
catch {*smooth elems 1 5}
```

如果要调整 triangle cleanup 的内部阈值，改：

```tcl
aspect=6.0 height=0.3
```

如果要调整 smooth 次数，改：

```tcl
*smooth elems 1 5
```

比如改成：

```tcl
*smooth elems 1 8
```

### 6.8 replace nodes 修复位置

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2138-2156 行
```

当前关键命令：

```tcl
hm_answernext yes
*replacenodes $move $keep 1 0
```

这是你手动验证过的“合并/移动节点消除坏三角形”的修复方式。

### 6.9 tetra 生成命令参数

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2310-2318 行
```

当前核心命令：

```tcl
*freesimulation
*createstringarray 2 "pars: upd_shell fix_comp_bdr vol_skew='0.700000,0.800000,0.600000,1.000000,0.860000,0.990000'" "tet: 67 1.3 -1 0 0.8 -1 -1"
*createmark components 2 "$target_component"
*tetmesh components 2 1 elements 0 -1 1 2
```

这部分是你手动 tetra 成功后反推回来的参数。一般不建议频繁改，除非确认
HyperMesh 手动生成记录里有更合适的一组参数。

### 6.10 tetra 体网格质量检查和修复阈值

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2330-2371 行
```

当前质量检查：

```tcl
*elementtestvolumetricskew elems 1 $repair_vol_skew 1 0 ""
```

其中 `$repair_vol_skew` 默认来自：

```python
repair_vol_skew: float = 0.99
```

位置见第 6.1。

当前体网格修复顺序：

```text
1. solid_mesh_optimization
2. smooth_3
3. smooth_8
4. smooth_15
```

如果要调体网格 smooth 次数，修改：

```tcl
*smooth elems 1 3
*smooth elems 1 8
*smooth elems 1 15
```

### 6.11 drag 六面体尺寸和贴合度参数

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
generate_batched_drag_hex_tcl(...)
约第 2808-2810 行
```

当前参数：

```python
element_size: float = 1.5
fit_tolerance_ratio: float = 0.05
retry_count: int = 2
```

含义：

- `element_size`：drag 六面体目标尺寸。
- `fit_tolerance_ratio`：drag 生成后 bbox 贴合度检查比例。
- `retry_count`：drag 失败后的重试次数。

### 6.12 drag 尺寸限制和自动计算规则

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2895-2897 行
```

当前逻辑：

```tcl
set thickness_size [expr {$dd/4.0}]
set source_size [expr {min($source_minor/3.0, $source_major/8.0)}]
set cs [expr {max(0.5, min(1.5, min($elem_size, $thickness_size, $source_size)))}]
```

含义：

- `$dd/4.0`：按厚度估算尺寸。
- `$source_minor/3.0`：按源面较小方向估算尺寸。
- `$source_major/8.0`：按源面较大方向估算尺寸。
- `max(0.5, min(1.5, ...))`：最终尺寸限制在 `0.5..1.5`。

如果想允许 drag 更细，可以改 `0.5`。

如果想允许 drag 更粗，可以改 `1.5`。

### 6.13 drag 贴合度检测

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 2873-2881 行
```

当前逻辑：

```tcl
set t [expr {max($z*1.5,$d*$r)}]
for {set i 0} {$i<6} {incr i} {
    if {abs([lindex $eb $i]-[lindex $sb $i])>$t} {return 0}
}
```

含义：

- `$z*1.5`：按 drag 网格尺寸给基础容差。
- `$d*$r`：按实体 bbox 对角线和 `fit_tolerance_ratio` 给比例容差。
- 两者取较大值。

### 6.14 实体分类时影响 tetra 最小尺寸的参数

文件：

```text
hypermesh_mcp_server.py
```

位置：

```text
约第 221-224 行
```

当前参数：

```python
TETRA_COMPLEX_SURFACE_COUNT = 50
TETRA_VERY_COMPLEX_SURFACE_COUNT = 120
TETRA_COMPLEX_MIN_ELEMENT_SIZE = 0.25
TETRA_VERY_COMPLEX_MIN_ELEMENT_SIZE = 0.20
```

含义：

- 面数量超过 `50` 会按复杂实体处理，允许更小的最小尺寸。
- 面数量超过 `120` 会按非常复杂实体处理，允许更小的最小尺寸。

具体使用位置：

```text
classify_all_solids_from_probe(...)
约第 1251-1264 行
```

### 6.15 离线 runner 默认传入 drag 参数的位置

文件：

```text
run_full_meshing_workflow.py
```

位置：

```text
约第 322-324 行
```

当前逻辑：

```python
element_size=args.drag_element_size
fit_tolerance_ratio=args.drag_fit_tolerance_ratio
retry_count=args.drag_retry_count
```

如果你想让离线版本默认 drag 尺寸改掉，也可以改这个文件中参数定义的位置：

```text
run_full_meshing_workflow.py
约第 460-463 行
```

当前默认值：

```python
parser.add_argument("--drag-element-size", type=float, default=1.0)
parser.add_argument("--drag-fit-tolerance-ratio", type=float, default=0.05)
parser.add_argument("--drag-retry-count", type=int, default=2)
```

注意：这里改的是离线脚本默认传参；`hypermesh_mcp_server.py` 里改的是底层生成器默认值。

## 4. 查看输出结果

最终模型默认保存到：

```text
E:\mcp\hypermesh-mcp-server\outputs\full_mesh.hm
```

流程日志和统计文件保存在：

```text
E:\mcp\hypermesh-mcp-server\runs\
```

常用结果文件：

- `workflow_report_<时间戳>.txt`

中文报告内容包括：

- 本次检测到几个实体。
- 分类后几个是 drag，几个是 tetra。
- 最终总单元数、shell 残留数、tet4 数量、hex8 数量。
- 每个部件的划分方式和具体尺寸参数，包括 drag/tetra 的尺寸上下限、实际使用尺寸、贴合比例、重试或质量参数。
- 2D 面网格 aspect 修复统计。
- 每个发生修复的 solid 的修复过程。
- 失败批次或异常记录。

如果运行时加了 `--write-json`，才会额外生成：

- `workflow_latest_summary.json`
- `workflow_repair_summary_<时间戳>.json`
- `workflow_final_save_response_<时间戳>.json`

## 5. 注意事项

- 离线流程运行过程中不要关闭 HyperMesh。
- 离线流程运行过程中不要重新 source 另一个 listener。
- 默认情况下，如果某个 tetra 批次失败，脚本会记录失败信息并继续后面的批次。
- 如果希望遇到第一个失败就停止，可以加参数：

```powershell
python run_full_meshing_workflow.py --stop-on-error --output outputs\full_mesh.hm
```
