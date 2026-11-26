# ⚡ Kaggle GPU 云端推理部署指南 (Applio RVC + BS-Roformer)

> 本部署包专为 **AstrBot 翻唱与变声插件 (`astrbot_plugin_cucumber_applio_console`)** 设计，用于将耗费算力的「人声伴奏分离」和「Applio RVC 翻唱变声」卸载到 Kaggle 提供的 **GPU 算力** 上执行，同时本地专精保留高频低延迟的 TTS 语音，实现极致的**混合云架构**。

---

## 一、核心亮点与机制解析

### 1. 为什么推荐 BS-Roformer 分离模型？
* **模型全称**：`BS-Roformer-Viperx-1297` (`model_bs_roformer_ep_317_sdr_12.9755.ckpt`)
* **行业地位**：目前 AI 音乐分离界公认的 **SOTA 巅峰之作**，SDR（信噪失真比）高达 **12.98 dB**（远超旧版 UVR-MDX-Net 的 ~10.5 dB 与 Demucs v4 的 ~9 dB）。
* **音质优势**：
  * **人声零电音**：高频泛音与唇齿音完整保留，绝无传统算法的金属电音或水下闷罐感。
  * **伴奏零残留**：重低音、鼓点与合成器彻底切除，伴奏轨干干净净，没有哪怕一丝人声漏音。
* **双 T4 算力表现**：在 Kaggle 2x Tesla T4 的 FP16 浮点加速下，30 秒的副歌人声分离仅需 **4 ~ 6 秒**！

### 2. 怎样下载最省梯子流量？（少用/不用梯子流量的三种方案）
* **方案 A（强烈推荐，消耗 0 KB 本地梯子流量）**：
  * **直接在 Kaggle Notebook 内部下载**！
  * Kaggle 机器位于 Google Cloud 欧美机房，内网直连 Hugging Face 速度为 **80~150 MB/s**。
  * 脚本运行 `wget` 下载仅需 3 秒，**全程跑的是 Kaggle 服务器的流量，完全不走你家宽带或梯子的一兵一卒**！
* **方案 B（国内高速免梯镜像，若你想下载到本机）**：
  * **HF-Mirror（国内 Hugging Face 高速直连镜像，免翻墙）**：
    * 权重模型 (`.ckpt`)：[https://hf-mirror.com/vipfactory/Roformer-Models/resolve/main/model_bs_roformer_ep_317_sdr_12.9755.ckpt](https://hf-mirror.com/vipfactory/Roformer-Models/resolve/main/model_bs_roformer_ep_317_sdr_12.9755.ckpt)
    * 配置文件 (`.yaml`)：[https://hf-mirror.com/vipfactory/Roformer-Models/raw/main/model_bs_roformer_ep_317_sdr_12.9755.yaml](https://hf-mirror.com/vipfactory/Roformer-Models/raw/main/model_bs_roformer_ep_317_sdr_12.9755.yaml)
    * 备选模型 (ep_368)：[https://hf-mirror.com/ZFTurbo/Music-Source-Separation-Training/resolve/main/models/model_bs_roformer_ep_368_sdr_12.9628.ckpt](https://hf-mirror.com/ZFTurbo/Music-Source-Separation-Training/resolve/main/models/model_bs_roformer_ep_368_sdr_12.9628.ckpt)
* **方案 C（Kaggle Dataset 一次挂载，0 秒冷启动）**：
  * 在 Kaggle 点击 `+ New Dataset`，将下载好的模型和自定义音色（`.pth` / `.index`）上传为私有数据集。
  * 在 Notebook 右侧侧边栏点击 `+ Add Input` 挂载，容器启动时秒级只读挂载，连下载等待都不需要。

---

### 3. 真实处理耗时与全链路性能对比

AI 翻唱流水线包含 5 大环节：无损音频下载解码 -> BS-Roformer 深度人声伴奏分离 -> RMVPE 音高提取 -> Applio/RVC 变声合成 -> 多抽头立体声混响与混音。

* **云端全流程耗时（Dual Tesla T4 GPU 加速，约 1.6x 实时比率）**：
  * **30 秒副歌片段**：约 **40 ~ 50 秒**（分离 ~20s + 变声合成 ~15s + 下载混音 ~10s）。
  * **全首歌曲完整版（如 5 分 20 秒）**：约 **8 ~ 9 分钟（约 500~530 秒）**。
  * **纯 RVC 变声单步**：若仅计算模型变声步骤，约需 10~15 秒。
* **本地 CPU 对比（普通多核 CPU 设备）**：
  * **30 秒副歌片段**：约 **3.5 分钟（约 210 秒）**。
  * **全首歌曲完整版**：约 **25 ~ 35 分钟**。
  * 相比本地 CPU，云端双卡 T4 GPU 翻唱全流程提速达 **4 ~ 6 倍**！

---

### 4. 队列无任务自动关机机制（防空耗 GPU 时长）
* 云端 GPU 运行通常具有配额时长限制。如果脚本一直挂着不关机，开着放一晚就会把额度消耗殆尽。
* 本项目部署包内置了 **`IdleWatchdog`（看门狗守护线程）**：
  * **精准监控**：每一笔翻唱任务完成时记录最新时间戳；
  * **空闲倒计时**：若连续 **10 分钟（600秒）** 队列没有任何新任务，看门狗会立即触发：
    ```
    ⏰ [看门狗] 已经持续空闲 600 秒无任何翻唱任务！
    ⏰ 正在自动杀死容器关机退出，停止扣减云端 GPU 时长...
    ```
  * 自动执行 `os._exit(0)` 杀死进程。Kaggle 检测到容器主进程退出后，会**立刻停止会话并停止计费**！
* **本地无缝兜底**：
  * 当 Kaggle 关机休眠时，QQ 群如果有人点歌，AstrBot 插件探测到云端未开启，会**自动无缝降级回本地执行**，绝不中断机器人的正常响应！

---

## 二、三步极速部署步骤

### 第一步：导入 Notebook 到 Kaggle
1. 登录 [Kaggle](https://www.kaggle.com/)（如果没有账号先注册一个，需在 Settings 绑定手机号以解锁免费 GPU 权限）。
2. 点击页面左侧的 **+ Create** -> **New Notebook**。
3. 点击顶部菜单栏的 **File** -> **Import Notebook**，上传本地的 `kaggle_applio_runner.ipynb`。

### 第二步：检查并开启右侧侧边栏配置 (Notebook options)
在 Notebook 页面右侧面板中，进行以下关键设置：
1. **Accelerator (加速器)**：选择 **`GPU T4 x2`**（必须选双 T4）。
2. **Internet (联网开关)**：必须打开并勾选 **`Internet on`**（否则无法连通公网）。

### 第三步：点击 Run All 并复制公网地址
1. 点击顶部工具栏的 **Run All**（或按快捷键 `Ctrl + F10`）。
2. 容器开始自动化部署（约耗时 1 分钟拉取环境与模型）。
3. 观察最后一个单元格（Cell 5）的输出日志，等待输出绿色的公网隧道地址：
   ```
   ======================================================================
   🎉 Kaggle 云端推理网关已成功连通公网！
   🔗 隧道地址 (Kaggle Endpoint): https://xxxx-xxxx-xxxx.trycloudflare.com
   ======================================================================
   ```
4. 复制该 `https://*.trycloudflare.com` 地址。

---

## 三、在 AstrBot 插件中启用

1. 打开 AstrBot 控制台 -> 进入 **插件设置** -> 找到 **cucumber_applio_console**。
2. 找到 **算力与执行架构 (architecture)** 区域：
   * **推理执行架构模式**：选择 **`混合云模式 (Kaggle云端翻唱 + 本地TTS)`**
   * **Kaggle Webhook / API 地址**：粘贴刚才复制的 Cloudflare 隧道地址（例如 `https://xxxx.trycloudflare.com`）
   * **Kaggle 云端超时时间**：保持默认 `300` 秒（留足冷启动与排队余量）
3. 点击 **保存配置**。
4. 现在去 QQ 群发送 `#翻唱 晴天`，即可体验 Kaggle 双 T4 GPU 的秒级翻唱！
