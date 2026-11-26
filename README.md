# astrbot_plugin_cucumber_applio_console · Applio 翻唱与语音控制台

<div align="center">

![Applio 控制台](logo.png)

**AstrBot 现代化 AI 翻唱 + TTS 语音回复全功能控制台**

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.16+-4A90E2.svg?style=flat-square&logo=bot)](https://github.com/AstrBotDevs/AstrBot)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-orange.svg?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-aiocqhttp%20%7C%20qq__official-green.svg?style=flat-square)]()
[![Gateway](https://img.shields.io/badge/Gateway-Docker%20%7C%20GHCR-brightgreen.svg?style=flat-square&logo=docker)](https://github.com/AkinoSa/cucumber-applio-gateway)
[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12-blue.svg?style=flat-square&logo=python)]()

翻唱《歌名》-> 自动多音源检索 -> 副歌智能裁剪 -> 人声分离 -> Applio RVC 变声 -> 卷积混响 -> 语音发回群聊  
@Bot 我想听你说… -> 人格情绪文本生成 + 角色台词经 TTS -> Applio 变声拟人输出

![访客统计](https://m.h4ck.org.cn/get/@cucumber_applio_console?theme=rule34)

</div>

> [!NOTE]
> 本插件为纯调度与交互端，模型推理、人声分离等重型任务由独立部署的 **[cucumber-applio-gateway](https://github.com/AkinoSa/cucumber-applio-gateway)** 推理网关（Docker 预构建镜像开箱即用）承载；同时支持对接 Kaggle 免费提供的 Dual Tesla T4 GPU 算力进行云端高速推理，并在云端休眠或离线时自动无缝回退本地。

---

## 项目概述

一个面向 AstrBot 生态打造的开箱即用 AI 音乐与语音套件：

- **群聊 AI 翻唱**：群内发送「`翻唱《晴天》`」或「`/rvc 晴天 3`」，机器人自动完成多平台搜索选歌、副歌能量裁剪、高保真人声伴奏分离、Applio RVC 模型音色转换、真实卷积混响润色并以群语音形式发出。
- **自然语言与 LLM 智能点歌解析**：
  - 具备模糊口语识别能力，直接对机器人说「`Bot，我想听你唱孙燕姿的我不难过`」即可自动提取歌手与歌名发起翻唱任务。
  - 智能识别时长意图：提及「副歌」、「片段」、「高潮」、「唱一段」或未明确指明时长时，默认采用**副歌模式**（精选高潮片段，耗时短，体验优）；明确提及「全首」、「整首」、「完整版」时，自动触发**全曲模式**进行整首歌翻唱。
  - 智能识别人声/伴奏意图：默认采用人声与伴奏完整融合输出；当明确提及「清唱」、「纯人声」时，自动切换为纯人声输出。
- **TTS 拟人角色语音回复**：发送「`@Bot 我想听你说...`」，大语言模型生成带有角色性格的情绪台词，结合 AstrBot 配置的 TTS 服务商生成基础音频，并通过 Applio RVC 变声为你设定的角色声线后语音回复。
- **原生 WebUI 控制台**：内嵌于 AstrBot 管理面板，涵盖系统概览看板、当前任务追踪与双队列监控、RVC 在线工作台、TTS 试音台、模型动态管理、8 参数专业级卷积混响调音面板与一键自检诊断。
- **云地混合弹性算力架构**：支持无独立 GPU 的低功耗服务器或常规主机纯 CPU 运行，并可无缝接入 Kaggle Dual Tesla T4 GPU 云端算力（云端固定且强制搭载 **BS-Roformer 1297** 分离模型）；配备空闲 10 分钟看门狗自动释放算力，并在云端休眠或断网时平滑回退本地 Docker 网关继续执行，保障服务永不下线。

---

## 实测性能基准与硬件对比

### 1. 测试硬件环境说明

- **测试宿主机（本地）**：
  - 处理器：i5 6500
  - 内存配置：16 GB RAM
  - 容器环境：Linux Docker / Container Station
- **云端算力节点（云端）**：Kaggle 免费算力容器
  - 计算硬件：Dual Tesla T4 GPU（双卡 16GB x 2 显存）
  - 运行环境：Python 3.10 / PyTorch CUDA / Applio 原生推理内核

### 2. 全链路耗时实测对比表

测试耗时涵盖完整处理链路：音频多源检索下载 -> 音频切片与响度平衡 -> BS-Roformer 人声伴奏分离 -> RMVPE 高精度音高提取与 Applio 模型变声 -> 音轨混音叠加 -> 最终音频编码与网络传输。

| 测试场景与音频类型 | 本地环境 (纯CPU) | 云端环境 (Dual Tesla T4 GPU) | 性能提升与特征说明 |
|---|---|---|---|
| **30 秒高潮副歌片段 (默认 +10s 缓冲寻优)** | 约 3 分 30 秒 ~ 4 分钟 | **约 40 ~ 50 秒** | 约 5 倍加速。自动在「目标时长 ~ 目标+缓冲」内贴合句尾换气点，云端响应迅捷 |
| **5 分 20 秒完整全曲** | 约 28 ~ 35 分钟 | **约 8 分 48 秒 (528 秒)** | 约 4 倍加速。长音频人声分离与变声无需本地 CPU 满载发热 |
| **TTS 拟人短句语音合成** | 约 10 ~ 15 秒 | **约 3 ~ 6 秒** | 即时交互无感，对话体验逼真流畅 |

---

## Kaggle 云端算力与混合架构

为了让没有独立高性能显卡的本地主机或低功耗服务器用户也能享受秒级 AI 翻唱，本插件原生支持对接 Kaggle 云端 Dual Tesla T4 GPU 算力通道：

- **免费高性能算力**：调用 Kaggle 每周免费提供的 **30 小时 Dual Tesla T4 GPU** 算力，长音频人声分离与 Applio 变声由云端承担，大幅降低本地 CPU 满载与发热。
- **全自动远程冷启动与本地兜底**：通过配置 Kaggle API Token，群内点歌时若云端处于关机休眠状态，插件会自动远程唤醒冷启动；若网络中断或配额耗尽，自动平滑回退至本地 Docker 网关继续执行，服务不中断。
- **闲置看门狗自动关机**：云端后台常驻 10 分钟（600 秒）闲置看门狗守护进程，无任务超时即自动终止释放算力，冻结每周免费 GPU 额度。
- **云端 Notebook 源码**：启动所需的 Notebook 文件已内置于本插件仓库：`kaggle/kaggle_applio_runner.ipynb`。

> [!TIP]
> **详细配置引导**：  
> 插件内嵌的 WebUI 控制台提供了完整的图文使用说明（涵盖 Cloudflare 专属隧道搭建、Kaggle API 令牌获取、一键冷启动联调及常见问题排查）。  
> 安装插件后，打开 AstrBot 管理后台中的「**Applio 控制台 -> 使用说明**」即可查看保姆级图文配置指南，无需在 README 翻阅冗长教程。

---

## 部署与安装

### 第一步：启动 Docker 推理网关（`cucumber-applio-gateway`）

无论是否启用 Kaggle 云端加速，**强烈建议**先在本地 / NAS 通过 Docker 部署配套推理网关 [cucumber-applio-gateway](https://github.com/AkinoSa/cucumber-applio-gateway)（预构建镜像已配置为 GHCR 开箱即用，负责本地 RVC/Applio 翻唱兜底与毫秒级 TTS 角色变声）：

1. **准备模型资产目录**（在宿主机新建目录，例如 `/path/to/rvc/`）：
   - `weights/`：【必填】你的 RVC / Applio 音色模型文件（`*.pth`）
   - `indices/`：【可选】对应的检索特征索引文件（`*.index`，按模型名自动匹配）
   - `pymss_weights/`：【必填】人声伴奏分离模型文件（如 `3_HP-Vocal-UVR.pth`）
   - `hubert_base/`：【必填】HuBERT / ContentVec 声学特征提取模型
   - `rmvpe/`：【必填】RMVPE 高精度音高提取模型

2. **拉取网关仓库并启动容器**：
   ```bash
   git clone https://github.com/AkinoSa/cucumber-applio-gateway.git
   cd cucumber-applio-gateway
   cp .env.example .env
   # 编辑 .env，填写上面模型目录、工作区 RVC_WORK_DIR 与 AstrBot data/plugin_data 路径
   nano .env

   docker compose pull && docker compose up -d
   ```
   > 预构建镜像默认使用 `ghcr.io/akinosa/cucumber-applio-gateway:1.0.0`（完整指南见 [cucumber-applio-gateway README](https://github.com/AkinoSa/cucumber-applio-gateway)）。如需从源码自构建，可取消 `compose.yaml` 中 `build: .` 的注释并执行 `docker compose up -d --build`。

3. **验证网关连通**：
   ```bash
   curl http://127.0.0.1:3333/
   # 返回 HTTP 200 页面即表示 Docker 网关启动成功
   ```

---

### 第二步：安装 AstrBot 插件

在 AstrBot 插件目录下克隆本项目（或下载 Release 压缩包解压至 `AstrBot/data/plugins/`）：

```bash
cd AstrBot/data/plugins
git clone https://github.com/AkinoSa/astrbot_plugin_cucumber_applio_console.git
```

在 AstrBot WebUI 中刷新并加载插件，依赖库将自动就绪。

---

### 第三步：配置插件与服务商

进入 **AstrBot WebUI → Applio 控制台 → 插件设置**（或 AstrBot 原生插件配置页）：

1. **网关服务地址**（`rvc.rvc_base_url`）：填写 `http://127.0.0.1:3333/`（若 AstrBot 与网关跨容器或跨主机，请填宿主机局域网 IP 与端口，**末尾必须带 `/`**）。
2. **网关工作目录**（`advanced.gateway_work_dir`）：填写与网关 `.env` 中 `RVC_WORK_DIR` **完全一致的宿主机绝对路径**（用于群语音发送、历史回放与任务音频读取）。
3. **（可选）TTS 角色语音**：在 AstrBot「服务商配置」中添加一个 `text_to_speech` 服务商（如 **Edge TTS** 或 **fish.audio**），并在插件设置「TTS 服务商 ID」下拉框中选择即可。
4. **（可选）Kaggle 混合云**：打开控制台内「**使用说明 → Kaggle 与域名穿透说明**」，按图文引导填入 Kaggle API Token 与 Cloudflare 隧道域名即可开启双卡 T4 云端加速。
5. 发送群指令 `/刷新rvc模型`，确认能列出 `weights/` 中的音色即完成配置。

---

## 聊天点歌与控制指令

### 1. 翻唱点歌指令

| 指令格式 | 示例 | 行为说明 |
|---|---|---|
| 自然语言点歌 | `Bot，我想听你唱孙燕姿的我不难过` | LLM 自动提取歌名与歌手，默认采用副歌模式与人声伴奏完整结合 |
| 指定全曲翻唱 | `Bot，完整唱一遍晴天` / `翻唱整首晴天` | 智能识别全首需求，触发全曲模式进行长音频完整翻唱 |
| 指定清唱人声 | `Bot，我想听你清唱起风了` | 智能识别清唱需求，输出无伴奏纯 AI 变声人声 |
| 正则精准点歌 | `/rvc 晴天` | 直接检索并翻唱《晴天》副歌片段 |
| 指定音色点歌 | `/rvc 晴天 3` / `/rvc 晴天 --voice 音色名` | 使用指定音色模型翻唱歌曲 |
| 本地音频翻唱 | 引用群内音频并发送 `/本地翻唱` | 直接对群内上传的语音或音频文件进行 RVC 变声处理 |

### 2. 任务状态与排队管理

| 指令 | 示例 | 说明 |
|---|---|---|
| `/cover_status` | `/cover_status` | 查看当前正在运行的任务进度、用时与排队位置 |
| `取消翻唱任务` | `取消翻唱任务` | 取消由自己提交的排队中或执行中的任务 |

### 3. 管理员控制指令

| 指令 | 说明 |
|---|---|
| `/刷新rvc模型` | 触发网关重新扫描 `weights` 目录下的新音色并同步至列表 |
| `/查看翻唱缓存` | 查看当前已缓存的伴奏、人声切片与成品占用体积 |
| `/清理翻唱缓存` | 释放已生成的中间音频切片与缓存文件 |
| `/设置默认音色 <模型名>` | 设置全局默认翻唱音色 |

---

## WebUI 控制台功能

本插件深度集成了 AstrBot 原生扩展管理界面，在管理后台点击本插件即可打开图形化控制台：

1. **概览看板**：实时展示 Gateway 本地与云端联通状态、当前推理任务、音色模型总数与最近任务简报。
2. **任务监控**：RVC 翻唱队列与 TTS 语音队列双栏呈现，显示 task_id、耗时分解、排队状态及中断控制。
3. **RVC 在线工作台**：免输入聊天指令，在网页端直接上传音频、调节调音参数并试听变声结果。
4. **TTS 试音台**：在线输入文本、选择预设人格声线与混响，一键试听角色合成效果。
5. **模型管理**：可视化重命名音色别名、标记男女声倾向、单独为 RVC 与 TTS 勾选启用状态。
6. **音频效果（混响）**：8 参数专业卷积混响调音面板，附带试听波形与房体尺寸调节。
7. **诊断与自检**：一键检测网关连接、工作目录读写权限、音源 API 可用性与依赖完整度。

---

## 常见问题排查 (FAQ)

<details>
<summary><b>1. 发送「翻唱《歌名》」机器人只文字答应，但没有实际翻唱？</b></summary>

自然语言翻唱依赖 AstrBot 的大语言模型工具调用（Function Calling）能力：
1. 检查你当前使用的 LLM 模型是否支持工具调用（推荐 OpenAI GPT-4o、Claude 3.5、DeepSeek-V3 等主流模型）；
2. 亦可直接使用绝对确定的指令格式：`/rvc 歌名`，此命令完全不依赖大模型，直接触发执行。
</details>

<details>
<summary><b>2. 提示「网关连接失败」或「Gateway 无法访问」？</b></summary>

1. 确认本地 Docker 网关已成功运行（`docker ps` 查看容器状态是否为 Up）；
2. 检查配置中的访问地址是否以斜杠 `/` 结尾（例如 `http://127.0.0.1:3333/`）；
3. 若 AstrBot 与网关分别运行在不同容器或主机，请填入局域网实际 IP，不能填 `127.0.0.1`；
4. 若启用了云端加速，检查 Kaggle Notebook 是否已启动并保持在线。
</details>

<details>
<summary><b>3. TTS 变声只有文字回复，没有发出语音？</b></summary>

1. 本插件不内置 TTS 生成 API，必须在 AstrBot「服务商配置」中添加一个 `text_to_speech` 提供商（例如 Edge TTS）；
2. 检查插件高级设置中 `gateway_work_dir` 填写的路径是否与网关宿主机工作路径完全一致；
3. TTS 拟人角色变声必须依赖本地 Docker 网关处理。若仅配置了云端 Kaggle 而未部署本地网关，TTS 变声将无法使用（系统将自动降级输出原版 TTS 语音）。
</details>

<details>
<summary><b>4. 只配置了 Kaggle 云端加速，不部署本地 Docker 网关可以吗？</b></summary>

可以，但存在功能限制与容灾风险：
1. **翻唱功能**：只要云端 Kaggle 处于开机在线状态，群聊点歌翻唱 100% 正常运行，不需要本地独立显卡，甚至无需本地 Docker 网关；
2. **TTS 角色变声受限**：TTS 属于高频低延迟的实时对话场景，由本地网关常驻处理。若未部署本地网关，TTS 变声功能不可用，将自动降级为输出无变声的原版 TTS 语音；
3. **缺少离线兜底保底**：当 Kaggle 自动关机休眠、账号每周 30 小时 GPU 配额耗尽或云端网络波动时，由于没有本地 Docker 网关接管，翻唱任务将无法执行。强烈建议在本地部署轻量 Docker 网关作为双重保障。
</details>

---

## 致谢与开源许可

本项目基于 **AGPL-3.0** 许可证开源，继承上游项目的开源义务与衍生授权：

- **上游核心派生**：[sdfsfsk/matsuko_cover](https://github.com/sdfsfsk/matsuko_cover) 与 [void-HaoNan/astrbot-plugin-chorus-cover](https://github.com/void-HaoNan/astrbot-plugin-chorus-cover)；
- **核心推理框架**：[Applio](https://github.com/IAHispano/Applio) 与 [Retrieval-based-Voice-Conversion-WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)；
- **人声分离核心**：[BS-Roformer](https://github.com/ZFTurbo/Music-Source-Separation-Training) 与 PyMSS 系模型；
- **机器人底层驱动**：[AstrBot](https://github.com/AstrBotDevs/AstrBot) 机器人框架。

感谢所有开源社区贡献者的持续付出与支持。
