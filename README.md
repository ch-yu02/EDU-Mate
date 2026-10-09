# EDU-Mate

EDU-Mate 是一个本地课堂助手：通过麦克风记录授课语音，通过摄像头保存课件或板书，将课堂内容整理成字幕、笔记和知识图谱，并提供课后总结、待办、自测和基于课堂资料的问答。

## 工作方式

- WhisperLive 使用本地 OpenVINO 模型生成字幕，默认运行在 Intel GPU 上。
- 本地 Qwen 在 CPU 上分批整理结构化笔记，不改写实时字幕。
- 后端调用配置的云端模型分析图片、更新图谱、生成课后产物和辅助问答。
- React 前端控制课堂开始和结束，通过 WebSocket 接收更新。
- 每节课的资料保存在 `data/sessions/`，检索支持默认词法检索和可选 LlamaIndex 向量检索。

结束课堂会先保存记录，然后继续生成最终笔记。最终笔记完成后，最终图谱与总结、待办等产物分两路处理。图谱的 `final` 表示最终笔记图谱更新成功，不是简单的“已结束”标记，也不是知识点完整性的质量保证。

## 从哪里开始

首次部署请阅读 [新设备部署指南](docs/DEPLOYMENT_GUIDE.md)。当前桌面入口依赖已安装的 Python、Node.js、OpenVINO、硬件驱动和模型，还不是一个独立安装包。

环境准备好后，在项目根目录运行：

```bash
scripts/dev.sh app
```

默认打开 `http://127.0.0.1:4173`。在页面中开始课堂后，麦克风链路接入该课堂；摄像头需要浏览器授权。图片分析需要支持图片输入的云端模型。

创建桌面快捷方式：

```bash
scripts/dev.sh desktop-shortcut
```

开发时可使用 `scripts/dev.sh dev`，但它只启动前后端，不会自动启动麦克风链路。所有可用命令可通过 `scripts/dev.sh help` 查看。

## 文档分工与公开范围

公开仓库保留使用、开发和接口所需的文档。各文档只维护自己的主题，避免多处重复记录相同流程。

| 文档 | 用途 | 是否公开 |
| --- | --- | --- |
| `README.md` | 项目入口、文档导航和公开范围 | 是 |
| [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | 原理概览、安装、配置、验收和迁移 | 是 |
| [LLM_PROVIDER_SETUP.md](docs/LLM_PROVIDER_SETUP.md) | 云端模型、超时、代理和向量检索配置 | 是，仅使用占位凭据 |
| [API_SCHEMA.md](docs/API_SCHEMA.md) | HTTP、WebSocket、事件和数据格式 | 是，示例不代表真实课堂记录 |
| [AGENTS.md](AGENTS.md) | 代码目录、开发约定和维护指引 | 是 |
| `Tasks.md` | 本地开发进度与待办清单 | 否，保留本地并停止 Git 跟踪 |
| 原 `docs/PACKAGING_PLAN.md` | 与部署指南重复的封装说明 | 已合并删除，不再单独维护 |
| `docs/COMPETITION_REPORT_OUTLINE.md` | 比赛报告写作草稿 | 否，保留本地并由 Git 忽略 |
| `docs/COMPETITION_REPORT_SECTION_2_9_2_11.md` | 比赛章节草稿，引用本地测试图片 | 否，保留本地并由 Git 忽略 |

`.env.example` 是公开配置模板；真实 `.env`、课堂资料、调试日志、检索索引、模型、依赖目录以及 `figures/` 下的本地图片不作为公开文档提交。原选题和设计方案材料也继续由 `.gitignore` 排除。

## 当前边界

程序主要面向本地 Linux 与 Intel OpenVINO 环境。新硬件上的实时性能仍需验证，自动化测试不能代替麦克风、摄像头及云端 API 的真实测试。

课后任务目前运行在服务进程内，关闭程序会中断未完成任务，重启后不会自动恢复执行。`GET /health` 只表示后端存活，不代表语音、模型或外设都已就绪。完整的安装包、依赖锁定、进程监管和任务恢复仍在开发计划中。
