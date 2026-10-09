# EDU-Mate 项目说明与新设备部署指南

本文介绍 EDU-Mate 的工作方式，以及如何在一台新设备上从零部署。内容根据当前代码和原开发设备的环境整理。

目前项目已经有桌面启动入口，但还不是自带运行环境和模型的独立安装包。首次部署需要安装系统工具、Python 和前端依赖，准备本地模型，并配置云端 API。完成这些工作后，日常使用可以通过桌面图标启动。

> 本文的部署步骤尚未在一台全新设备上完整验收。安装成功、网页打开和课堂全链路正常，是三个不同的验收阶段。

## 1. 项目能做什么

EDU-Mate 是一个以课堂为单位保存资料的本地课堂助手。用户在前端开始一节课后，可以通过麦克风记录授课内容，也可以拍摄课件或板书。系统将这些内容整理为字幕、笔记和知识图谱，供课堂中查看和课后查询。

| 组成部分 | 负责的工作 |
| --- | --- |
| React 前端 | 开始和结束课堂，展示字幕、摄像头、图片分析、图谱、课后产物和问答 |
| FastAPI 后端 | 管理课堂状态，接收数据，推送页面更新，保存资料并调度 Agent |
| WhisperLive + OpenVINO | 在本地把麦克风音频转成字幕，默认使用 Intel GPU |
| 本地 Qwen | 在 CPU 上将字幕整理为结构化课堂笔记 |
| 云端 LLM | 分析图片、生成知识图谱、总结、待办和课堂名称，并辅助基于课堂资料的问答 |
| 检索模块 | 从字幕、笔记、图片分析和图谱中检索资料，支持词法检索和可选的向量检索 |

本地 Qwen 和云端模型是两套不同的配置。修改云端 `LLM_MODEL`，不会改变本地负责笔记的 Qwen 模型。

### 课堂中的数据流

音频链路：

```text
麦克风 → ffmpeg 采集 → WhisperLive 转录
                         ├─ 临时字幕 → 前端预览
                         └─ 正式字幕 → 保存并显示
                                        ↓
                               本地 Qwen 分批整理笔记
                                        ↓
                               云端 Agent 更新知识图谱
```

临时字幕会随着识别结果变化，正式字幕才进入课堂记录。结束课堂时，前端会先尝试把当前有用的临时字幕提交为正式字幕，避免最后一句直接丢失。本地 Qwen 负责笔记，不再改写前端的实时字幕。

图片链路：

```text
浏览器摄像头 → 拍照并保存本地 → 云端多模态分析
                                      ↓
                         图片描述、要点、知识图谱和检索资料
```

拍照按钮和 `Ctrl+1` 快捷键均保留。内置图片链路直接调用多模态模型，不要求先做 OCR。分析失败时会记录错误；课堂仍在进行时，前端可以自动重试。

### 结束课堂后的数据流

点击结束后，后端先保存课堂并返回，较慢的处理继续在后台完成：

```text
保存课堂 → 完成最终 Qwen 笔记 → 立即发布 notes_ready
                                    ├─ 最终知识图谱更新
                                    └─ 总结、待办、命名及索引相关处理
```

最终图谱与课后产物是两路独立任务；产物任务内部的总结和待办目前仍然顺序生成。图谱的 `final` 表示基于最终笔记的图谱更新成功，不是单纯表示课堂已结束，也不代表模型保证提取了每一个知识点。

这些后台任务仍依附于运行中的服务进程。结束课堂后可以等待产物完成，但不要把关闭整个程序理解为任务会在系统后台继续执行。目前中断的产物任务会被标记为失败，并非自动恢复执行。

## 2. 部署前需要准备什么

以下步骤以 Ubuntu 24.04 x86_64 为例，采用 Python 3.12、Node.js 24 和受 OpenVINO 支持的 Intel 核显。其他系统或硬件需要单独确认兼容性，尤其不能直接把当前 Intel GPU 和 ALSA 音频方案照搬到任意 ARM 开发板。

- 安装好系统并可正常联网的新设备。
- 能被系统识别的麦克风和摄像头。
- 支持所用 Intel GPU 的驱动。
- 可用的云端 API 地址、模型名称和 API Key。要使用图片分析，模型和接口必须支持图片输入。
- 足够容纳模型、依赖、图片和课堂记录的磁盘空间。

GPU 驱动需要单独安装。`pip install openvino` 不会替你安装操作系统的 GPU 驱动，具体步骤应按设备型号和系统版本参考 [OpenVINO 的 Intel GPU 配置说明](https://docs.openvino.ai/2026/get-started/install-openvino/configurations/configurations-intel-gpu.html)。

## 3. 安装代码和基础依赖

先安装 Node.js 24 及 npm。可参考 [Node.js 官方下载入口](https://nodejs.org/en/download)，安装后确认终端可以找到它们：

```bash
node -v
npm -v
```

然后安装系统工具并获取代码：

```bash
sudo apt update
sudo apt install git python3.12-venv python3-pip ffmpeg alsa-utils v4l-utils xdg-utils
git clone https://github.com/ch-yu02/EDU-Mate.git
cd EDU-Mate
python3.12 -m venv .venv
scripts/dev.sh install-backend
npm ci --prefix frontend
cp .env.example .env
```

后续命令除非特别说明，都在 `EDU-Mate` 项目根目录运行。`cp .env.example .env` 只用于首次部署；已有配置时不要重复覆盖。

项目使用两套 Python 环境：项目下的 `.venv` 运行后端，另一套 OpenVINO 环境运行 WhisperLive 和本地 Qwen。不要只安装其中一套。

## 4. 安装本地推理环境

下面使用当前用户目录下的 `openvino` 作为模型工作目录。OpenVINO 版本取自原开发设备，目的是提供一个复现起点，不代表已对所有新设备做过兼容性验证。

```bash
python3.12 -m venv "$HOME/openvino/venv"
"$HOME/openvino/venv/bin/python" -m pip install \
  "openvino==2026.2.0" \
  "openvino-genai==2026.2.0.0" \
  "openvino-tokenizers==2026.2.0.0" \
  huggingface_hub
"$HOME/openvino/venv/bin/python" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
OPENVINO_ROOT="$HOME/openvino" scripts/dev.sh install-whisperlive
```

安装脚本使用 WhisperLive 0.9.0，并只补装当前 OpenVINO 路径需要的部分依赖。它假设 OpenVINO Python 已经存在，因此不能用这一条命令代替上面的环境准备。

检查 OpenVINO 是否能识别 GPU：

```bash
"$HOME/openvino/venv/bin/python" -c 'import openvino as ov; print(ov.Core().available_devices)'
```

输出中应包含 `GPU` 或类似 `GPU.0` 的设备。只有 `CPU` 时，先检查驱动和设备权限；默认启动路径要求 GPU 可见。CPU 回退可以用于诊断，但不能据此判断目标设备上的实时性能已经达标。

## 5. 下载本地模型

当前默认使用两份模型：

| 用途 | 模型 | 保存位置 |
| --- | --- | --- |
| 语音识别 | `OpenVINO/whisper-large-v3-turbo-fp16-ov` | `~/.cache/openvino_whisper_models/whisper-large-v3-turbo-fp16-ov` |
| 结构化笔记 | `OpenVINO/Qwen2.5-Coder-3B-Instruct-int4-ov` | `~/openvino/qwen2.5-3b-int4` |

注意：原开发设备的 `qwen2.5-3b-int4` 目录实际存放的是 **Coder 版本**，这一点已通过模型 README 确认。WhisperLive 的默认 FP16 模型也不是旧测试脚本使用的 `whisper-large-v3-turbo-int8-ov` 目录。复现环境时不要混淆这两条语音测试路径。

可以使用下面的命令预先下载模型：

```bash
"$HOME/openvino/venv/bin/python" - <<'PY'
from pathlib import Path
from huggingface_hub import snapshot_download

home = Path.home()
snapshot_download(
    "OpenVINO/Qwen2.5-Coder-3B-Instruct-int4-ov",
    local_dir=str(home / "openvino/qwen2.5-3b-int4"),
)
snapshot_download(
    "OpenVINO/whisper-large-v3-turbo-fp16-ov",
    local_dir=str(home / ".cache/openvino_whisper_models/whisper-large-v3-turbo-fp16-ov"),
)
PY
```

也可以完整复制旧设备上的对应模型目录。不要只复制权重文件，配置、分词器及相关文件也需要保留。Python 虚拟环境建议在新设备重建。

## 6. 配置路径和云端 API

编辑项目根目录的 `.env`，增加下面的路径配置，并检查同名设置不要重复：

```dotenv
OPENVINO_ROOT=$HOME/openvino
OPENVINO_PYTHON=$HOME/openvino/venv/bin/python
QWEN_DEVICE=CPU
WHISPERLIVE_MODEL=OpenVINO/whisper-large-v3-turbo-fp16-ov
APP_ENABLE_MIC=1
APP_ENABLE_QWEN_NOTES=1
APP_ENABLE_CLOUD_GRAPH=1
APP_MIC_AUDIO_DEVICE=auto
APP_MIC_LANGUAGE=auto
```

启动脚本会读取这个文件并展开 `$HOME`。显式配置路径很重要，否则代码默认会到原开发设备的 `/home/edu-mate_user/openvino` 下寻找环境和模型。

当前课堂中的 Qwen 默认读取 `$OPENVINO_ROOT/qwen2.5-3b-int4`，建议首次部署保留这个目录结构。仅设置 `QWEN_MODEL` 不能保证课堂中和课后两条路径同时切换模型，因为两处读取方式还没有完全统一。

云端配置可以通过向导完成：

```bash
scripts/dev.sh llm-config
```

也可以直接修改 `.env` 中的以下字段：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_MODEL=供应商提供的模型名称
LLM_BASE_URL=供应商提供的兼容接口地址
LLM_API_KEY=你自己的API密钥
```

上面的中文值是占位说明，必须替换为真实配置。图片分析要求模型支持图片输入；文本请求成功不能证明图片请求也可用。供应商配置的更多说明见 [LLM_PROVIDER_SETUP.md](LLM_PROVIDER_SETUP.md)。

配置时还需要注意：

- `.env.example` 是模板，真正使用的是 `.env`。模板中的空 API Key 不能直接调用云端服务。
- 已导出的环境变量优先于 `.env`。终端中遗留的同名变量可能覆盖刚修改的文件。
- `LLM_IGNORE_PROXY=1` 表示云端请求忽略系统代理，直接连接服务。网络需要代理才能访问该服务时，应按实际情况调整。
- 修改 `.env` 后需要完整重启程序。API Key 不应放到前端配置，也不要提交到 Git。

## 7. 检查外设并启动程序

先检查系统能否发现外设：

```bash
arecord -l
v4l2-ctl --list-devices
```

麦克风默认自动选择，优先匹配 USB 或麦克风类设备。选错时，依据 `arecord -l` 的结果把 `.env` 中的 `APP_MIC_AUDIO_DEVICE` 设置为对应的 `plughw:X,Y`。不要直接照搬旧设备的声卡编号，重新插拔后编号也可能变化。

启动完整应用：

```bash
scripts/dev.sh app
```

默认前端地址为 `http://127.0.0.1:4173`，后端为 `http://127.0.0.1:8000`，WhisperLive 端口为 `9090`。首次使用摄像头时需要允许浏览器访问。

`app` 会启动后端、构建后的前端、WhisperLive 和麦克风进程。麦克风等待前端创建课堂后才开始向该课堂发送字幕。本地 Qwen 按笔记处理需要加载，不是一个单独常驻的 API 服务。

`scripts/dev.sh dev` 只启动开发用的前后端，不会自动启动整套麦克风链路。测试完整桌面流程时应使用 `app`。

确认命令行启动正常后，创建桌面入口：

```bash
scripts/dev.sh desktop-shortcut
```

之后可以点击桌面的 EDU-Mate 图标启动。如果桌面系统提示未信任启动器，需要允许启动。项目目录移动后，应重新生成快捷方式。

## 8. 怎样判断部署成功

先检查代码和云端文本调用：

```bash
scripts/dev.sh test
scripts/dev.sh build
scripts/dev.sh llm-smoke
```

自动化测试主要使用模拟依赖，不能替代真实外设和模型验收。随后进行一次真实课堂测试：

1. 在前端点击开始课堂，确认麦克风进程连接到这节课。
2. 连续讲述一段内容，确认临时字幕出现，随后成为正式字幕。
3. 检查课堂进行中是否产生有实际内容的结构化笔记，而不只是字幕列表。
4. 拍摄一张清晰的课件或板书，确认图片保存、分析和图谱更新成功。
5. 检查字幕中独有的知识点是否进入图谱，避免只验证图片来源。
6. 点击结束课堂，等待最终笔记、最终图谱、总结、待办和自动命名的状态分别完成。
7. 重新打开历史课堂，确认资料能读取，Agent 能依据课堂内容回答问题。

遇到问题时，优先按数据链路定位：

| 现象 | 首先检查 |
| --- | --- |
| 页面正常，但没有字幕 | 启动方式是否为 `app`、OpenVINO 环境是否存在、GPU 是否可见、麦克风是否选对 |
| 有字幕，但没有笔记正文 | session 下的 `qwen_notes_debug.jsonl`，以及本地模型是否成功加载 |
| 有笔记，但没有图谱更新 | 云端配置、图谱请求错误及超时信息 |
| 图片已保存，但分析失败 | 图片返回的错误、模型是否支持图片、服务连接和超时设置 |
| 课后产物一直未完成 | `post_class_status.json` 中具体停在哪个阶段，而不是只看总体状态 |

## 9. 可选：启用向量检索

建议先跑通课堂流程，再启用向量检索。默认词法检索依赖较少，可以作为第一阶段的配置。

```bash
scripts/dev.sh install-rag
```

然后将 `.env` 中这两个设置改为：

```dotenv
RAG_QUERY_BACKEND=llamaindex
GLOBAL_SEARCH_BACKEND=llamaindex
```

重启应用并验证：

```bash
scripts/dev.sh rag-smoke --require-llamaindex
scripts/dev.sh rebuild-global-index --llamaindex
```

首次使用还可能下载 embedding 模型。测试时应检查是否真正使用向量检索，以及是否出现回退词法检索的警告。不带 `--llamaindex` 的重建命令只重建文档快照。

## 10. 数据迁移和当前限制

每节课的资料保存在 `data/sessions/<session_id>/`，包括字幕、结构化笔记、图片、图谱、总结、待办、状态和调试记录。需要迁移历史课堂时，应在旧服务停止写入后完整复制这些目录。全局检索索引位于 `data/indexes/`，可以根据课堂数据重新构建。

Git 仓库不包含真实 `.env`、课堂记录、模型、Python 虚拟环境和前端依赖。因此只执行 `git clone`，无法复现旧设备上的完整运行环境。

目前尚未完成完整的依赖版本锁定、自动部署、后台任务持久化恢复和所有子进程的状态监控。桌面入口提供的是便捷启动，不是环境安装器。新设备是否能跟上授课速度，仍需要通过真实音频、图片和较长课堂测试确认。

后续封装方向包括：图形化首次配置与桌面窗口、模型和外设就绪检查、子进程故障展示、课后任务持久化恢复、运行环境打包，以及升级时的配置和数据迁移。现有 `/health` 只检查后端存活，不代表这些能力已经完成。

本地开发待办可记录在 `Tasks.md`，该文件不随公开仓库分发，也不是部署所需文件。

相关文档：

- [项目开发指南](../AGENTS.md)
- [API 与输入数据约定](API_SCHEMA.md)
- [云端模型与检索配置](LLM_PROVIDER_SETUP.md)
- [项目入口与文档公开范围](../README.md)
