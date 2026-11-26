#!/usr/bin/env python3
"""Run a non-interactive Codex task via `codex app-server --listen stdio://`.

Threads created via `codex app-server` are persisted with `vscode` source
metadata so they appear in the Codex Desktop App session list while still
providing a single-shot CLI wrapper suitable for `codex-worker`.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any


def _find_codex_binary() -> str:
    """Locate the Codex CLI executable across Windows and POSIX platforms."""
    binary = shutil.which("codex.exe") or shutil.which("codex")
    if not binary:
        raise FileNotFoundError("未在 PATH 中找到 codex.exe 或 codex 可执行文件。")
    return binary


def _read_prompt(task_file: str | None) -> str:
    """Read the task prompt from a UTF-8 file or stdin."""
    if task_file and task_file != "-":
        return Path(task_file).read_text(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    return sys.stdin.read()


def _drain_stderr(stream: Any, tail: collections.deque[str]) -> None:
    """Drain child stderr in a background thread to prevent pipe deadlock."""
    try:
        for line in stream:
            tail.append(line.rstrip("\r\n"))
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _send_json(proc: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    """Write a single-line JSON-RPC message to the app-server stdin."""
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def _handle_server_request(proc: subprocess.Popen[str], msg: dict[str, Any]) -> None:
    """Reject unexpected interactive server requests so the worker never hangs."""
    _send_json(
        proc,
        {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "error": {
                "code": -32601,
                "message": "Non-interactive worker does not support interactive requests",
            },
        },
    )


def _read_message(proc: subprocess.Popen[str]) -> dict[str, Any]:
    """Read and parse the next JSON-RPC line from app-server stdout."""
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if line == "":
            raise EOFError("codex app-server 意外关闭了标准输出流。")
        stripped = line.strip()
        if not stripped:
            continue
        return json.loads(stripped)


def _wait_response(
    proc: subprocess.Popen[str], request_id: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read messages until the response matching `request_id` arrives."""
    buffered_notifications: list[dict[str, Any]] = []
    while True:
        msg = _read_message(proc)
        if "method" in msg and "id" in msg:
            _handle_server_request(proc, msg)
            continue
        if msg.get("id") == request_id:
            if "error" in msg and msg["error"] is not None:
                raise RuntimeError(f"JSON-RPC 请求失败 (id={request_id}): {msg['error']}")
            return msg.get("result") or {}, buffered_notifications
        if "method" in msg:
            buffered_notifications.append(msg)


def _extract_agent_text(items: list[dict[str, Any]]) -> str:
    """Return the last `agentMessage` text from a completed turn's items."""
    for item in reversed(items):
        if item.get("type") == "agentMessage":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


def _format_source(source: Any) -> str:
    """Render thread source metadata cleanly whether string or object."""
    if isinstance(source, str):
        return source
    if source is None:
        return ""
    return json.dumps(source, ensure_ascii=False)


MODEL_TIERS = {"luna", "terra", "sol", "astra"}


def _resolve_model(model_arg: str) -> str:
    """Resolve a tier alias (`luna`/`terra`/`sol`/`astra`) via local models_cache.json."""
    cleaned = model_arg.strip()
    tier = cleaned.lower()
    if tier not in MODEL_TIERS:
        return cleaned

    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    cache_path = codex_home / "models_cache.json"
    if not cache_path.is_file():
        raise RuntimeError(
            f"未找到模型缓存文件 {cache_path}，无法自动解析系列 '{cleaned}'，请先运行一次 codex 或显式传入完整模型名。"
        )

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    candidates = []
    for entry in data.get("models", []):
        slug = str(entry.get("slug") or "")
        tokens = slug.lower().replace("_", "-").split("-")
        if tier in tokens or slug.lower().endswith(f"-{tier}"):
            candidates.append(entry)

    if not candidates:
        raise RuntimeError(
            f"在 {cache_path} 中未找到 '{cleaned}' 系列模型，请显式传入完整模型名。"
        )

    listed = [m for m in candidates if m.get("visibility") == "list"]
    pool = listed or candidates
    pool.sort(key=lambda m: m.get("priority", 999999))
    return str(pool[0]["slug"])


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a single-shot Codex task via `codex app-server`."
    )
    parser.add_argument("-C", "--cd", default=None, help="工作区根目录（非 --resolve-only 时必填）")
    parser.add_argument(
        "-f", "--task-file", default=None, help="UTF-8 任务单文件路径（默认或 '-' 从 stdin 读取）"
    )
    parser.add_argument(
        "-o", "--output-last-message", default=None, help="最终答复落盘路径（UTF-8）"
    )
    parser.add_argument(
        "-s",
        "--sandbox",
        default="workspace-write",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="沙盒模式（默认 workspace-write）",
    )
    parser.add_argument(
        "-m",
        "--model",
        required=True,
        help="显式指定执行模型或系列档位（luna / terra / sol / astra），必选",
    )
    parser.add_argument(
        "--allow-astra",
        action="store_true",
        help="允许使用 Astra 系列高难模型（仅限用户手动确认后传入）",
    )
    parser.add_argument(
        "--resolve-only",
        action="store_true",
        help="仅解析并打印模型 slug 后退出（用于调试或回退命令获取实际模型名）",
    )
    parser.add_argument("-p", "--profile", default=None, help="Codex config profile")
    parser.add_argument("--resume", default=None, help="继续执行的已有 threadId")
    parser.add_argument(
        "--approval-policy",
        default="never",
        choices=["never", "on-request", "untrusted"],
        help="审批策略（默认 never）",
    )
    args = parser.parse_args()
    if not args.resolve_only and not args.cd:
        parser.error("the following arguments are required: -C/--cd")
    return args


def _normalize_cwd(path_str: str) -> str:
    """Return an absolute path with an uppercase Windows drive letter."""
    resolved = os.path.abspath(path_str)
    if len(resolved) >= 2 and resolved[1] == ":" and resolved[0].isalpha():
        resolved = resolved[0].upper() + resolved[1:]
    return resolved


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args()
    if "astra" in args.model.lower() and not args.allow_astra:
        print(
            f"已拦截对高难模型 '{args.model}' 的调用：Astra 仅限极高难度任务使用，必须先经用户手动确认并显式传入 --allow-astra。",
            file=sys.stderr,
        )
        return 2

    try:
        resolved_model = _resolve_model(args.model)
    except Exception as exc:
        print(f"模型解析失败: {exc}", file=sys.stderr)
        return 1

    if "astra" in resolved_model.lower() and not args.allow_astra:
        print(
            f"已拦截对高难模型 '{resolved_model}' 的调用：Astra 仅限极高难度任务使用，必须先经用户手动确认并显式传入 --allow-astra。",
            file=sys.stderr,
        )
        return 2

    if args.resolve_only:
        print(resolved_model)
        return 0

    cwd = _normalize_cwd(args.cd)
    prompt_text = _read_prompt(args.task_file)
    if not prompt_text.strip():
        print("任务单内容为空，已中止执行。", file=sys.stderr)
        return 1

    codex_bin = _find_codex_binary()
    cmd = [codex_bin]
    if args.profile:
        cmd.extend(["-p", args.profile])
    cmd.extend(["app-server", "--listen", "stdio://"])

    stderr_tail: collections.deque[str] = collections.deque(maxlen=50)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stderr is not None
    stderr_thread = threading.Thread(
        target=_drain_stderr, args=(proc.stderr, stderr_tail), daemon=True
    )
    stderr_thread.start()

    thread_id = ""
    source_str = ""
    actual_model = resolved_model
    status = "unknown"
    turn_error: Any = None
    last_message = ""
    delta_chunks: list[str] = []

    try:
        _send_json(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex-worker",
                        "title": "Codex Worker",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            },
        )
        _wait_response(proc, 1)

        _send_json(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "initialized",
                "params": {},
            },
        )

        thread_params: dict[str, Any] = {
            "cwd": cwd,
            "sandbox": args.sandbox,
            "approvalPolicy": args.approval_policy,
            "model": resolved_model,
        }

        if args.resume:
            thread_method = "thread/resume"
            thread_params["threadId"] = args.resume
        else:
            thread_method = "thread/start"
            thread_params["ephemeral"] = False
            thread_params["threadSource"] = "user"

        _send_json(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": thread_method,
                "params": thread_params,
            },
        )
        thread_result, _ = _wait_response(proc, 2)
        thread_obj = thread_result.get("thread") or {}
        thread_id = str(thread_obj.get("id") or args.resume or "")
        source_str = _format_source(thread_obj.get("source"))
        actual_model = str(thread_result.get("model") or thread_obj.get("model") or resolved_model)

        if not thread_id:
            raise RuntimeError(f"未能从 {thread_method} 响应中解析出 threadId: {thread_result}")

        _send_json(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "turn/start",
                "params": {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt_text}],
                },
            },
        )
        _, pending = _wait_response(proc, 3)

        def handle_notification(note: dict[str, Any]) -> bool:
            nonlocal last_message, status, turn_error
            method = note.get("method")
            params = note.get("params") or {}
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    text = item.get("text")
                    if isinstance(text, str):
                        last_message = text
            elif method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str):
                    delta_chunks.append(delta)
            elif method == "turn/completed" and params.get("threadId") == thread_id:
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "unknown")
                turn_error = turn.get("error")
                if not last_message:
                    last_message = _extract_agent_text(turn.get("items") or [])
                if not last_message and delta_chunks:
                    last_message = "".join(delta_chunks)
                return True
            return False

        done = False
        for note in pending:
            if handle_notification(note):
                done = True
                break

        while not done:
            msg = _read_message(proc)
            if "method" in msg and "id" in msg:
                _handle_server_request(proc, msg)
                continue
            if "method" in msg:
                done = handle_notification(msg)

    except Exception as exc:
        print(f"执行异常: {exc}", file=sys.stderr)
        if stderr_tail:
            print("codex app-server stderr 摘要:", file=sys.stderr)
            for line in stderr_tail:
                print(f"  {line}", file=sys.stderr)
        return 1
    finally:
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        stderr_thread.join(timeout=1)

    output_target = "(stdout)"
    if args.output_last_message:
        out_path = Path(args.output_last_message)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(last_message, encoding="utf-8")
        output_target = str(out_path)

    print(f"THREAD_ID: {thread_id}")
    print(f"MODEL: {actual_model}")
    print(f"SOURCE: {source_str}")
    print(f"STATUS: {status}")
    print(f"OUTPUT_FILE: {output_target}")
    if not args.output_last_message and last_message:
        print(last_message)

    if status != "completed":
        print(f"任务未正常完成 (status={status}, error={turn_error})", file=sys.stderr)
        if stderr_tail:
            print("codex app-server stderr 摘要:", file=sys.stderr)
            for line in stderr_tail:
                print(f"  {line}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
