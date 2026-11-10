# CLI 演示

以下命令从项目根目录的 PowerShell 7 执行。首次准备环境时，用 Python 3.12 创建虚拟环境并安装项目。

```powershell
py -3.12 -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

已有环境时直接执行激活命令。创建本地 `.env` 并在编辑器里填写 `DEEPSEEK_API_KEY`。已有文件会被保留，`.env` 受 Git 忽略规则保护。

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
notepad .env
```

## 新建 A/B 会话并检查隔离

```powershell
$sessionA = (python -m agent_harness.cli --new-session | ConvertFrom-Json).session_id
python -m agent_harness.cli --trace --session $sessionA "创建一条待办，内容是写论文"

$sessionB = (python -m agent_harness.cli --new-session | ConvertFrom-Json).session_id
python -m agent_harness.cli --session $sessionB "创建一条待办，内容是买牛奶"

python -m agent_harness.cli --session $sessionA
python -m agent_harness.cli --session $sessionB
python -m agent_harness.cli --session $sessionA "列出我的待办"

$sessionA
$sessionB
```

带消息的命令由模型决定是否调用工具，stdout 包含 `session_id`、`answer` 和 `status`。省略消息时直接从磁盘读取指定会话，返回 `session_id` 和 `todos`。用这两个列表核实 A 的“写论文”和 B 的“买牛奶”各自落盘，并保存打印出的两个 Session ID。

## 重新打开终端并继续操作

回到同一项目目录，重新激活环境。会话文件位于当前目录下的 `sessions/`。按提示粘贴上一节的两个 ID。

```powershell
. .\.venv\Scripts\Activate.ps1
$sessionA = Read-Host "A 的 Session ID"
$sessionB = Read-Host "B 的 Session ID"

$restoredA = python -m agent_harness.cli --session $sessionA | ConvertFrom-Json
$todoA = $restoredA.todos | Where-Object { $_.content -eq '写论文' }
if (-not $todoA) { throw 'A 中没有找到写论文待办' }
$todoId = $todoA.id
python -m agent_harness.cli --trace --session $sessionA "把 ID 为 $todoId 的待办内容改成明天写论文"
python -m agent_harness.cli --session $sessionA
python -m agent_harness.cli --session $sessionB

python -m agent_harness.cli --session $sessionA "删除 ID 为 $todoId 的待办"
python -m agent_harness.cli --session $sessionA
python -m agent_harness.cli --session $sessionB
```

修改后可核对 A 的 Todo ID 保持原值。删除后可核对 A 的列表为空、B 的待办仍在。用不同的 Session ID 发下一条命令即可切换。

## 查看 Trace 和退出码

`--trace` 位于 `--session` 前。stderr 每行是一条 JSON 事件，stdout 保留会话结果。查看同一 `request_id` 下的 `tool_requested`、`tool_started`、`tool_result` 和 `request_finished` 可以确认工具执行与请求状态。

```powershell
python -m agent_harness.cli --trace --session $sessionB "列出我的待办" 2> .\cli-trace.log
$LASTEXITCODE
Get-Content .\cli-trace.log | ForEach-Object { $_ | ConvertFrom-Json }
```

| 场景 | stdout | 退出码 |
| --- | --- | --- |
| 创建或读取会话成功 | 新会话 ID 或会话待办 JSON | `0` |
| 请求完成 | JSON，`status=completed` | `0` |
| 运行中失败或触顶 | JSON，`status=failed` 或 `status=limit_reached` | `1` |
| 会话不存在或配置检查失败 | 空，错误信息写入 stderr | `1` |
| 命令用法错误 | 空，用法错误写入 stderr | `2` |

下面的全零 ID 可以演示不存在的会话。启用 Trace 时，stderr 中的错误也可逐行解析为 JSON。

```powershell
python -m agent_harness.cli --trace --session ('0' * 32) "列出我的待办"
$LASTEXITCODE
```

触顶或运行中失败前可能已有工具写入。可用无消息的 `--session` 命令核对实际待办，再决定后续操作。
