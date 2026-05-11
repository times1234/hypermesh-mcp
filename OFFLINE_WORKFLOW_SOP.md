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

### 在无网络电脑上安装依赖

先确认无网络电脑上已经安装 Python。建议目标电脑使用和当前电脑一致的
Python 大版本和架构。

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

## 1. 打开 HyperMesh 并导入模型

手动打开 HyperMesh，然后导入需要划分网格的模型。

## 2. 生成 GUI Listener Tcl

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

## 3. 在 HyperMesh 中 source 这个 Tcl

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

## 4. 测试连接

回到 PowerShell，运行：

```powershell
python -c "import hypermesh_mcp_server as hm; print(hm.execute_tcl_gui('puts {PING_OK}', enforce_meshing_rules=False))"
```

如果返回内容里包含：

```text
PING_OK
```

说明 PowerShell 和 HyperMesh GUI 已经连通。

## 5. 运行离线划分流程

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
7. 在 `runs` 文件夹中生成流程日志和统计汇总。

## 6. 查看输出结果

最终模型默认保存到：

```text
E:\mcp\hypermesh-mcp-server\outputs\full_mesh.hm
```

流程日志和统计文件保存在：

```text
E:\mcp\hypermesh-mcp-server\runs\
```

常用结果文件：

- `workflow_latest_summary.json`
- `workflow_repair_summary_<时间戳>.json`
- `workflow_final_save_response_<时间戳>.json`

## 7. 注意事项

- 离线流程运行过程中不要关闭 HyperMesh。
- 离线流程运行过程中不要重新 source 另一个 listener。
- 默认情况下，如果某个 tetra 批次失败，脚本会记录失败信息并继续后面的批次。
- 如果希望遇到第一个失败就停止，可以加参数：

```powershell
python run_full_meshing_workflow.py --stop-on-error --output outputs\full_mesh.hm
```
