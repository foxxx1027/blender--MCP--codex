# Blender MCP — 让 AI 直接操控 Blender 的 3D 建模服务

基于 Model Context Protocol 的 Blender 集成：AI 客户端（Codex / Claude Desktop / Cursor）经标准 MCP 协议连接独立 Python 进程，再经帧协议指挥 Blender 内部插件执行建模、材质、灯光、动画、渲染等 91 种操作。所有 bpy 调用投递回 Blender 主线程，非线程安全问题零容忍。

> 本仓库为 [blender_mcp_new](https://github.com/2132660698/blender_mcp_new) 的完整修复与增强版（P0–P9 共 25 个补丁），在真实 Blender 3.6.0 与 5.2.3 LTS 双版本上端到端验证通过。

---

## 架构

```
┌──────────────┐  stdio(JSON-RPC)  ┌──────────────────┐  帧协议+token认证  ┌──────────────────┐
│  AI 客户端    │ ◄───────────────► │  MCP 服务器       │ ◄───────────────► │  Blender 插件      │
│ (Codex/Claude │                   │  (独立 Python 进程) │  TCP/UnixSocket   │  (bpy, 91 个工具)  │
│  /Cursor)    │                   │  start_server.py  │                   │                  │
└──────────────┘                   └──────────────────┘                   └──────────────────┘
```

三个组件各司其职：

| 组件 | 运行位置 | 职责 |
|---|---|---|
| **AI 客户端** | 用户侧（Codex / Claude Desktop / Cursor） | 理解自然语言，决定调用哪个工具、传什么参数 |
| **MCP 服务器** | 独立 Python ≥3.10 进程 | 协议翻译（MCP JSON-RPC ↔ 帧协议）、token 认证、连接管理、工具列表三级兜底 |
| **Blender 插件** | Blender 内部（合法模块名 `blender_addon`） | 91 个工具的真实 bpy 执行，全部投递回主线程 |

**为什么拆两个进程？** Blender 内置 Python 锁定在旧版本且无法安装现代依赖（mcp SDK ≥1.4.1 需要 Python ≥3.10）；独立进程可以用任意新版 Python，通过 IPC 指挥 Blender 内的插件。bpy 非线程安全，所有操作经 `run_in_main_thread` 投递回 Blender 主线程执行。

---

## IPC 帧协议

两端（MCP 服务器 ↔ Blender 插件）共用一份逐字节相同的编解码模块 `framing.py`（由单元测试强制校验一致性），线格式：

```
<字节长度>:<JSON payload>
```

关键设计决策：

- **按字节计数**（非字符数）——历史上两端按 `len(json_str)` 字符计数，只因 `json.dumps` 默认 `ensure_ascii=True` 才未触发中文消息的流错位；本版统一按 UTF-8 字节计数，根治该隐患
- **`FrameDecoder` 增量解码**——`feed()` 接受任意分块字节（半帧/一帧/多帧），内部缓冲拼接，适用于 asyncio 流与同步 socket 两种场景
- **协议错位显式报错**——缓冲区头部出现非法长度前缀时抛出 `FramingError`（流级致命），由调用方关闭/重置连接，不静默吞数据

---

## 91 个工具

### object_tools（7 个）
| 工具 | 说明 |
|---|---|
| `create_object` | 创建立方体/球体/平面/圆柱/圆锥/环体/空物体 |
| `delete_object` / `duplicate_object` / `rename_object` | 基础操作 |
| `select_object` / `transform_object` | 选中/变换（位置、旋转、缩放） |
| `set_object_mode` | 七种模式切换（OBJECT / EDIT / **SCULPT** / POSE / VERTEX_PAINT / WEIGHT_PAINT / TEXTURE_PAINT） |

### modeling_tools（6 个）
修改器增删应用、布尔运算（并/差/交）、合并对象、分离部件。

### mesh_tools（13 个）
环切、切刀、挤出面、细分、顶点位移、**几何节点五件套**（创建修改器 / 添加节点 / 连线 / 读树 / 设参，覆盖程序化建模全链路）、**UV 展开**（smart / cube / cylinder / sphere 四种方式）。

### material_tools（11 个）
基础参数设置（Principled BSDF 的颜色/金属度/粗糙度）+ **着色器节点编辑四件套**（添加节点 / 连接插槽 / 读取节点树 / 设置属性），支持 60+ 种着色器节点类型，跨版本候选映射处理 Blender 3.x → 5.x 的类型名变更。

### lighting_tools（5 个）
四种光源（点光/太阳光/聚光/面光）、HDRI 环境、世界环境光设置。

### camera_tools（5 个）
创建相机、设为活动相机、调参数（焦距/类型等）、设视角、渲染当前视角。

### scene_tools（14 个）
场景信息、**场景层级查询**（集合树+对象父子关系）、集合管理、渲染出图、保存/加载 .blend、导入导出模型、多场景管理。

### effect_tools（9 个）
粒子系统创建/修改、流体域、烟雾域、**合成节点编辑三件套**（添加节点 / 连接插槽 / 读取节点树）。

### animation_tools（20 个）
关键帧、插值设置、骨骼/绑骨/IK 约束、动作管理、运动路径、跟随路径动画、**布料/刚体/软体物理模拟**、烘焙动画。

### scripting_tools（1 个）
| 工具 | 说明 |
|---|---|
| `execute_blender_python` | **能力逃生门**——在 Blender 主线程执行任意 bpy 代码（bpy / mathutils / bmesh 已注入命名空间，`result` 变量取回数据，stdout 一并捕获）。雕刻笔刷、权重绘制、视频剪辑、蜡笔等全部无专用工具的长尾领域由此覆盖 |

---

## 安全：token 认证

本地 IPC 通道默认启用 token 认证（可在插件首选项关闭）：

- 插件启动服务器时生成 `secrets.token_hex(32)`，写入 socket 旁 / 临时目录的 `.token` 文件（权限 0600）
- MCP 服务器连接后自动读取并完成认证（token 优先级：`BLENDER_MCP_TOKEN` 环境变量 > `.token` 文件），无需人工介入
- 未认证连接的任何请求都会被拒绝（`unauthorized`）
- 校验使用 `hmac.compare_digest`，防时序攻击

---

## 资源读取分级

`blender://mesh/Cube` 默认返回 **summary**（统计+变换+材质，不构建 bmesh、不传逐顶点数据）；需要详细数据时用 `blender://mesh/Cube?detail=full`，上限 10000 顶点 / 20000 面，超限截断并带 `vertices_truncated` / `faces_truncated` 标记。

支持资源订阅：客户端发送 `{"action": "subscribe_resource", "uri": "blender://mesh/Cube"}` 后，对象变换/材质/灯光变化会自动推送 `resource_update` 帧（GUI 模式经 `bpy.app.timers` 驱动，headless 模式经主线程泵驱动）。

---

## 测试体系

| 测试文件 | 检查项 | 依赖 |
|---|---|---|
| `test_unit_framing.py` | 帧编解码格式、任意分块边界、协议错位恢复、双端副本一致性 | 零依赖 |
| `test_unit_serializer.py` | to_jsonable 各类转换、standardize_result 行为、模型对象处理 | 零依赖 |
| `test_unit_ipc_client.py` | token 认证握手四场景（正确/错误/免认证/无 token 被拒） | 零依赖（真 socket） |
| `test_unit_new_tools.py` | 材质节点四件套+场景层级的 schema 结构与参数校验 | 零依赖 |
| `test_unit_p9_tools.py` | 任意 Python/模式切换/GN 五件套/UV 的 schema 与校验 | 零依赖 |
| `test_e2e_full.py` | 端到端 47 项：插件启用→认证→建模→材质节点→合成→订阅推送→负向 | 需活体 Blender |
| `test_stdio_protocol.py` | stdio 协议纯净性（每行合法 JSON）、83→91 工具列表、真实调用 | 需活体 Blender |

零依赖单测共 **199 项**检查，无需 Blender / mcp SDK / pytest，`python3 tests/test_unit_*.py` 即可运行。

---

## CI（GitHub Actions）

三个 job：

1. **unit-tests** — Python 3.10 / 3.12 矩阵跑零依赖单测（199 项）
2. **build-addon** — 按合法模块名 `blender_addon` 打包插件 zip 并上传 artifact
3. **blender-smoke** — 下载 Blender 3.6 LTS → headless 安装启用插件 → 启动 IPC 服务器 → 帧协议客户端直连冒烟（探活/认证/工具列表/建模/读资源），Blender 日志作为 artifact 上传

---

## 快速开始

### 安装 Blender 插件

Blender → 编辑/偏好设置 → 插件 → 安装 → 选本仓库构建的 `blender-addon.zip`（顶层目录为合法模块名 `blender_addon`）→ 勾选启用。

### 准备 MCP 服务器环境

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp-server/requirements.txt   # mcp>=1.4.1,<2 + pydantic>=2
```

### 配置 AI 客户端

```json
{
  "mcpServers": {
    "blender": {
      "command": "<路径>/.venv/bin/python",
      "args": ["<路径>/start_server.py"]
    }
  }
}
```

### 使用

1. 打开 Blender → 3D 视图按 **N** → **MCP** 标签 → **启动服务器**
2. 再启动/重启 AI 客户端（顺序：先 Blender 后客户端）
3. 直接说："创建一个红色立方体，加个点光源，给材质加个噪声凹凸"

---

## 修复清单（P0–P9，25 个补丁）

| 阶段 | 补丁 | 主题 |
|---|---|---|
| P0 | 0001–0003 | 端点统一（Linux/macOS 走 TCP）、移除 `_call_tool_impl` 猴子补丁、钉住 mcp SDK 版本 |
| P1 | 0004–0007 | 序列化单一出口 `to_jsonable`、双端共享帧编码 `framing.py`、删序列化补丁群（`fix_tuple_format` / `fix_boolean_values`） |
| P2/P3 | 0008–0010 | CI 三段式、token 认证、mesh 资源 `detail_level` 分级限流 |
| P4 | 0011–0014 | 资源轮询移入 Blender 主线程计时器、mcp-server 端口解析去重/死导入清理/内部反射降级、工具列表三级兜底、README 重写 |
| P5 | 0015–0017 | 着色器节点编辑四件套、场景层级查询、49 项零依赖单测 |
| P6 | 0018–0021 | `create_result` 二参误用修复（成功结果 `isError` 为真值）、Linux `port:` 走 TCP、着色器 `bl_idname` 映射、headless 冒烟脚本三处实测缺陷修复 |
| P7 | 0022–0023 | 合成节点三件套修复（bl_idname 映射/5.x API 自适应/前缀）、headless 资源订阅推送修复（泵驱动轮询+订阅键名死 bug） |
| P8 | 0024 | stdio stdout 纯净性（诊断输出改 stderr）、tools/list 改走 action 路径（原走未实现的 method 导致永远回退硬编码兜底） |
| P9 | 0025 | 任意 Python 逃生门、模式切换、几何节点五件套、UV 展开 |

---

## 版本兼容

| 环境 | 状态 |
|---|---|
| Linux + Blender 3.6 / 5.2 + 任意 MCP 客户端 | ✅ 真机验证（47/47 e2e + 4/4 stdio） |
| Windows / macOS | 代码路径完整，未实测 |
| Blender 4.x | 介于两个已验证版本之间，理论兼容 |

---

## 许可证

MIT
