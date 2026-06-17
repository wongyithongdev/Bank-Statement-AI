"""
Tools 实现 — Anthropic ACI (Agent-Computer Interface) 原则 (2026)

原则：
- 工具为 agent 设计，不是为开发者设计（"put yourself in the model's shoes"）
- 每个工具有明确单一职责（targeted high-impact workflows）
- 错误信息具体可操作，不是 opaque error codes
- 结果过滤在工具内部完成（filter data BEFORE returning to model）
"""

import subprocess
import textwrap
import tempfile
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
TIMEOUT = 60  # 秒


# ── read_file ─────────────────────────────────────────────────────────────────

def read_file(path: str, start_line: int = None, end_line: int = None, max_chars: int = None) -> dict:
    """
    读取文件内容，支持按行范围或字符数截取，避免把整个大文件倒入 context。
    """
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / path
    if not p.exists():
        return {"success": False, "error": f"File not found: {p}", "path": str(p)}
    try:
        content = p.read_text(encoding="utf-8")
        total_lines = content.count("\n") + 1
        total_chars = len(content)

        if start_line is not None or end_line is not None:
            lines = content.splitlines()
            s = (start_line - 1) if start_line else 0
            e = end_line if end_line else len(lines)
            content = "\n".join(lines[s:e])

        if max_chars is not None and len(content) > max_chars:
            content = content[:max_chars] + f"\n...[truncated at {max_chars} chars, file has {total_chars} total]"

        return {
            "success": True,
            "content": content,
            "path": str(p),
            "total_lines": total_lines,
            "total_chars": total_chars,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "path": str(p)}


# ── write_file ────────────────────────────────────────────────────────────────

def write_file(path: str, content: str) -> dict:
    """写入文件（覆盖已有内容，自动创建父目录）。"""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / path
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"success": True, "path": str(p), "bytes_written": len(content.encode("utf-8"))}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── edit_file ─────────────────────────────────────────────────────────────────

def edit_file(path: str, old_string: str, new_string: str) -> dict:
    """
    在文件中精确替换第一个 old_string。
    如果 old_string 不存在则报错（不会静默失败）。
    """
    result = read_file(path)
    if not result["success"]:
        return result
    content = result["content"]
    if old_string not in content:
        # 提供上下文帮助 agent 修正
        preview = content[:300] + ("..." if len(content) > 300 else "")
        return {
            "success": False,
            "error": f"old_string not found in {path}. File preview: {preview}",
        }
    new_content = content.replace(old_string, new_string, 1)
    write_result = write_file(path, new_content)
    if write_result["success"]:
        return {"success": True, "path": str(Path(path)), "replaced": 1, "occurrences_remaining": new_content.count(new_string)}
    return write_result


# ── list_directory ────────────────────────────────────────────────────────────

def list_directory(path: str = ".", pattern: str = None) -> dict:
    """
    列出目录内容。支持 glob pattern 过滤。
    比用 run_python 跑 os.listdir() 快 10x，不消耗 subprocess。
    """
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / path
    if not p.exists():
        return {"success": False, "error": f"Directory not found: {p}"}
    if not p.is_dir():
        return {"success": False, "error": f"Not a directory: {p}"}
    try:
        if pattern:
            entries = list(p.glob(pattern))
        else:
            entries = list(p.iterdir())

        files = []
        dirs = []
        for entry in sorted(entries):
            if entry.is_dir():
                dirs.append({"name": entry.name, "type": "dir"})
            else:
                size = entry.stat().st_size
                files.append({"name": entry.name, "type": "file", "size_bytes": size})

        return {
            "success": True,
            "path": str(p),
            "dirs": dirs,
            "files": files,
            "total": len(dirs) + len(files),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── search_file ───────────────────────────────────────────────────────────────

def search_file(path: str, pattern: str, context_lines: int = 2, max_matches: int = 20) -> dict:
    """
    在文件中搜索正则或字符串 pattern，返回匹配行及上下文。
    比 read_file + 全文搜索节省 90%+ context tokens。
    """
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / path
    if not p.exists():
        return {"success": False, "error": f"File not found: {p}"}
    try:
        content = p.read_text(encoding="utf-8")
        lines = content.splitlines()
        matches = []
        for i, line in enumerate(lines):
            try:
                if re.search(pattern, line, re.IGNORECASE):
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    matches.append({
                        "line_number": i + 1,
                        "matched_line": line,
                        "context": "\n".join(
                            f"  {'→' if j == i else ' '} {j+1}: {lines[j]}"
                            for j in range(start, end)
                        ),
                    })
                    if len(matches) >= max_matches:
                        break
            except re.error:
                # 如果 pattern 不是有效正则，做字面量搜索
                if pattern.lower() in line.lower():
                    matches.append({"line_number": i + 1, "matched_line": line})
                    if len(matches) >= max_matches:
                        break

        return {
            "success": True,
            "path": str(p),
            "pattern": pattern,
            "match_count": len(matches),
            "matches": matches,
            "total_lines": len(lines),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── run_python ────────────────────────────────────────────────────────────────

def run_python(code: str, working_dir: str = None) -> dict:
    """
    在沙盒子进程中运行 Python 代码。
    超时 60 秒，捕获 stdout / stderr。
    """
    if working_dir is None:
        working_dir = str(PROJECT_ROOT)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["python3", tmp_path],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=working_dir,
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        )
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timeout after {TIMEOUT}s — split into smaller steps"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        os.unlink(tmp_path)


# ── dispatch ──────────────────────────────────────────────────────────────────

def dispatch(tool_name: str, tool_input: dict) -> dict:
    """分发工具调用到对应函数，统一错误处理。"""
    try:
        if tool_name == "read_file":
            if "path" not in tool_input:
                return {"success": False, "error": "missing required param: path"}
            return read_file(
                tool_input["path"],
                start_line=tool_input.get("start_line"),
                end_line=tool_input.get("end_line"),
                max_chars=tool_input.get("max_chars"),
            )
        elif tool_name == "write_file":
            if "path" not in tool_input or "content" not in tool_input:
                return {"success": False, "error": "missing required params: path, content"}
            return write_file(tool_input["path"], tool_input["content"])
        elif tool_name == "edit_file":
            if not all(k in tool_input for k in ("path", "old_string", "new_string")):
                return {"success": False, "error": "missing required params: path, old_string, new_string"}
            return edit_file(tool_input["path"], tool_input["old_string"], tool_input["new_string"])
        elif tool_name == "list_directory":
            return list_directory(tool_input.get("path", "."), tool_input.get("pattern"))
        elif tool_name == "search_file":
            if "path" not in tool_input or "pattern" not in tool_input:
                return {"success": False, "error": "missing required params: path, pattern"}
            return search_file(
                tool_input["path"],
                tool_input["pattern"],
                context_lines=tool_input.get("context_lines", 2),
                max_matches=tool_input.get("max_matches", 20),
            )
        elif tool_name == "run_python":
            if "code" not in tool_input:
                return {"success": False, "error": "missing required param: code"}
            return run_python(tool_input["code"], tool_input.get("working_dir"))
        else:
            return {"success": False, "error": f"Unknown tool: {tool_name}. Available: read_file, write_file, edit_file, list_directory, search_file, run_python"}
    except Exception as e:
        return {"success": False, "error": f"Tool dispatch error: {e}"}
