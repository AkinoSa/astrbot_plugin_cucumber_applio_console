# Release Notes — astrbot_plugin_cucumber_applio_console v1.0.0

首个正式公开发布版本。由 [sdfsfsk/matsuko_cover](https://github.com/sdfsfsk/matsuko_cover) v2.13.0 派生重构，并在前代 RVC 控制台架构上全面升级为 **Applio v2 原生推理引擎 + 现代化原生 WebUI + Kaggle 混合云架构**。

## 功能亮点

- **AI 翻唱流水线**：多音源检索（网易云 / 酷狗 / 酷我 / QQ）→ 歌词重复定位与人声频段能量副歌裁剪（默认 +10s 句尾缓冲浮动寻优）→ 人声伴奏分离 → Applio RVC 变声（RMVPE / 自动音准 / 共振峰调节）→ 8 参数真实 FFT 卷积混响 → 语音/文件发送
- **TTS 拟人角色语音回复**：支持群聊 @Bot、关键词及引用消息快速朗读；动态读取 AstrBot `text_to_speech` 服务商，TTS 与翻唱模型/参数/混响完全隔离
- **现代化原生 WebUI 控制台**：概览看板（本地/混合云一键切换）、双队列任务监控（当前进行/排队实时进度 + 历史检索回放）、Applio 在线工作台、TTS 试音台、模型管理、8 参数混响调音、内置保姆级使用说明、配置同步与系统诊断
- **云地混合弹性调度**：支持纯本地 Docker 网关保底运行，以及无缝对接 Kaggle Dual Tesla T4 GPU 云端算力（远程冷启动、防重队列守护、闲置看门狗自动关机、云端离线平滑回退本地）
- **多会话并发任务注册表**：FIFO 队列、`task_id` 状态机、防重复提交、`/cover_status`、按用户软/硬取消

## 依赖环境

- **[cucumber-applio-gateway](https://github.com/AkinoSa/cucumber-applio-gateway)** v1.0.0+（配套独立 Docker 推理网关，支持从 GHCR 拉取 `ghcr.io/akinosa/cucumber-applio-gateway:1.0.0` 开箱即用）
- **AstrBot** `>= 4.16`（推荐 4.29+），如需自然语言点歌建议配合支持 Function Calling 的 LLM 提供商

## 兼容性与已知说明

- 本地网关主要在 Linux x86_64 CPU（及 Container Station / fnOS Docker）环境验证；云端模板支持 Kaggle `GPU T4 x2`
- WebUI 管理后台面向管理员使用，Token / Cookie 等配置支持实时明文回显与清空保存
- 若从早期私有版本或 `astrbot_plugin_chorus_cover` 升级，旧名数据与扁平配置会在启动时自动幂等迁移
