# DeepSeek 小鲸鱼桌宠（独立版）

DeepSeek Balance Whale Widget 的**独立桌面宠物版** —— 不再依赖浏览器 / DSH Web 界面。

桌面右下角的透明置顶小鲸鱼：实时显示 DeepSeek API 余额 + 今日已用 + 每轮对话消耗，
可通过内置 **API 中转**（OpenAI 兼容）统计每一次 chat/completions 调用的真实 token 消耗。

## 与 DSH 版的区别

| | DSH 插件版（原仓库） | 本独立版 |
|---|---|---|
| 运行环境 | DSH Web 界面（浏览器） | 独立 exe / Python 进程 |
| 余额拉取 | DSH 宿主的凭据服务 | 自定义配置（首次启动填入） |
| 每轮对话消耗 | 监听 DSH 会话事件 | **内置 API 中转**：客户端把 base_url 指向 `http://127.0.0.1:11434/v1`，中转转发到 DeepSeek 并统计 usage |
| 今日已用 | 小鲸鱼记账 / 实时令牌 | 相同（记账默认 / 令牌可选） |
| 配置 | `~/.dsh/` | `%APPDATA%\WhalePet\config.json`（便携：exe 同目录 config.json） |

## 快速开始（Python 运行）

```powershell
pip install -r requirements.txt
python main.py
```

首次启动弹出设置窗口，填入 **DeepSeek API Key**（必需，用于拉余额 + 中转转发），
保存后会自动缓存到 `%APPDATA%\WhalePet\config.json`，之后不再询问。

## 打包为独立 exe

```powershell
build.bat
```

产物：`dist\WhalePet.exe`（单文件，含资源）。首次运行同样弹出配置窗口。

## 使用方式

- **查看余额**：点击小鲸鱼 → 气泡显示余额 + 今日已用（5 秒自动收起），再点一次切换随机台词，再点一次关闭。
- **拖拽**：按住小鲸鱼拖动，松手后自动吸附到最近的四分之一边缘（四边 + 角组合）；吸附到左缘时整体镜像（文字保持可读）。
- **按压**：按下有小 Q 弹 + 音效（小黄鸭 / 音效1，可在设置中切换、调音量）。
- **自动刷新**：每 60 秒静默刷新；余额变化时数字滚动 + 弹气泡。
- **右键菜单 / 托盘**：设置、刷新余额、显示/隐藏、退出。

## 让 AI 客户端走中转（统计每轮消耗）

任何 OpenAI 兼容客户端把 `base_url` 改为：

```
http://127.0.0.1:11434/v1
```

示例（OpenAI SDK）：

```python
from openai import OpenAI
client = OpenAI(api_key="<任意值>", base_url="http://127.0.0.1:11434/v1")
resp = client.chat.completions.create(model="deepseek-chat", messages=[...])
```

中转会把请求转发到 `config.json` 里的 `api_base`（默认 `https://api.deepseek.com`），
从响应 usage 按**峰谷定价**（工作日高峰 9-12/14-18 点；2026-08-23 起周末全天谷价）
换算成本，小鲸鱼随即弹出「上一轮对话消耗 ¥X.XX」。

- 流式请求（`stream=True`）也支持：边转发边收集 usage。
- `GET /v1/models`、`GET /healthz` 可用于联通性检查。

## 目录结构

```text
Whale-Pet/
├── main.py                 # 入口（GUI + 中转服务启动 + 首次配置引导）
├── build.bat               # PyInstaller 打包脚本
├── requirements.txt        # PySide6 + requests
├── pet/
│   ├── config.py           # 配置持久化（首次填入后缓存）
│   ├── pricing.py          # 峰谷定价（移植自原插件 lib/index.js）
│   ├── balance.py          # 余额拉取 + 小鲸鱼记账账本 + 平台令牌用量
│   ├── proxy.py            # 独立 API 中转（/v1/chat/completions 转发 + usage 统计）
│   ├── whale_widget.py     # 桌宠窗口（气泡/拖拽/吸附/按压/音效/台词）
│   └── ui/settings_dialog.py # 设置对话框
├── assets/                 # 鲸鱼图 / gif / 音效（复制自原插件）
└── ledger.json             # 运行后自动生成：今日已用账本
```

## 说明

- 记账模式（默认）：每天首次启动时缓存当日**资金初始值**，用「观测到的余额下降」累计今日已用；
  账本持久化，重启后拿上次观测值与当前余额作差，**程序关闭期间的消耗也能补上**。
- 每轮对话消耗走中转统计，**立刻叠加**到「今日已用」（不等 60 秒轮询）；
  轮询同步后再用 `(+0.12)` / `(-0.04)` 标出与余额实际消耗的偏离值（没统计到任何一轮时不显示）。
- 实时·令牌模式：在设置中填入 DeepSeek 平台会话令牌（`Bearer eyJ...`）后，直接调平台用量接口精确换算。
- 定价常量在 `pet/pricing.py`，DeepSeek 调价时可直接修改。
