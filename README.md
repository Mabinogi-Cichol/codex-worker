# codex-worker

一个用于 Agent 协同编码的调度与验收技能：由主 AI 担任**架构师兼质检员**，将自然语言需求编译为强边界、高精度任务单交由 Codex 执行，并在完成后进行基线比对、越界清理与验收汇报，避免大模型在缺少详尽约束时出现过度设计、过度防御或越界修改。

## 核心特性

- **强边界任务单与质检清理**：执行前记录 Git / SVN 基线快照并锁定最小文件白名单；执行后自动核对 diff，精准回滚白名单外的越界修改，且不误伤用户原有未提交改动。
- **Codex App 原生会话联动**：内置 [`scripts/codex_app_exec.py`](scripts/codex_app_exec.py)，通过官方 `codex app-server --listen stdio://`（JSON-RPC 2.0）派发任务，会话自动同步至 Codex Desktop App 侧边栏，支持随时打开查看或通过 `codex resume <thread_id>` 接管。
- **零轮询静默等待**：利用进程退出自动唤醒机制替代轮询检查，配合 `-o` 将最终答复落盘，显著减少等待与日志回读的 Token 消耗。
- **按整体难度动态选型与 Astra 保护**：不写死具体模型版本号，按系列档位（`terra` / `sol` / `astra`）从本机 `~/.codex/models_cache.json` 自动解析最新可用模型；普通任务仅允许 `terra` / `sol`，高难模型 `astra` 必须经用户手动确认并传入 `--allow-astra` 方可启用。
- **小任务免调度提议**：当判断任务工作量过小、明显追不上跨 Agent 调度固定开销时，主动向用户提议由当前 AI 直接处理。

## 环境要求

- Python 3.9+（仅依赖标准库，无需安装第三方包）
- 已安装并完成登录的 `codex-cli`（支持 `codex app-server`）

## 安装与使用

将本仓库克隆到你的 Agent 技能目录（例如 `~/.gemini/config/skills/codex-worker`）：

```bash
git clone https://github.com/Mabinogi-Cichol/codex-worker.git ~/.gemini/config/skills/codex-worker
```

> **触发方式**：本技能仅由用户手动显式调用（例如输入 `/codex-worker` 或明确要求使用 `codex-worker` / `codex-cli` 执行任务），常规编码任务不会自动触发。

## 调度脚本独立用法

`scripts/codex_app_exec.py` 也可以在命令行中独立作为单次 `codex app-server` 调度器使用：

```bash
python scripts/codex_app_exec.py -C /path/to/workspace -m sol -s workspace-write -f task.txt -o result.txt
```

### 常用参数

| 参数 | 说明 |
| :--- | :--- |
| `-C`, `--cd <DIR>` | 工作区根目录（非 `--resolve-only` 时必填） |
| `-m`, `--model <MODEL>` | 必选，支持系列别名（`terra` / `sol` / `astra` / `luna`）或完整模型名 |
| `-f`, `--task-file <PATH>` | UTF-8 任务单文件路径；不传或传 `-` 时从 `stdin` 读取 |
| `-o`, `--output-last-message <PATH>` | 将 Codex 最终答复写入指定 UTF-8 文件 |
| `-s`, `--sandbox <MODE>` | 沙盒权限：`read-only`、`workspace-write`（默认）、`danger-full-access` |
| `--resume <THREAD_ID>` | 在已有会话上继续执行纠偏轮次 |
| `--allow-astra` | 显式放行 Astra 系列高难模型（未传时调用 Astra 会被直接拦截） |
| `--resolve-only` | 仅解析并打印实际生效的模型名称后退出 |

## 目录结构

```text
codex-worker/
├── .gitignore
├── README.md
├── SKILL.md
└── scripts/
    └── codex_app_exec.py
```
