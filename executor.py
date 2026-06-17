"""
Generator Agent (Executor) — Anthropic ACI + Harness Engineering 最佳实践 (2026)

改进点（对照 Anthropic 官方文档）：
1. 工具描述含 examples / edge cases / timeout — "put yourself in the model's shoes"
2. 新增 list_directory + search_file — targeted tools vs full-file dumps
3. run_python 串行执行，只读工具并行 — 防止并发写入竞争
4. Skills catalog 改为 Progressive Disclosure — 按需读取，不前置加载
5. Scratchpad (scratch.md) — 外部持久记忆，context 清除后不失忆
6. JSON summary 用 ###SUMMARY### marker 精确解析，不靠 rindex
7. 重复失败检测 — 同工具同参数连续失败 3 次则中止循环
8. 更早的 context 清除触发点（50K）+ 每轮必清（不只阈值触发）
"""

import json
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import tools as tool_module
import llm_client


MAX_TOOL_ROUNDS = 30
CONTEXT_CLEAR_THRESHOLD = 50_000   # tokens，更早触发清除
KEEP_LAST_TOOL_RESULTS  = 5
MAX_CONSECUTIVE_FAILURES = 3       # 同工具同参数连续失败中止

# ── 工具定义（Anthropic ACI 原则：描述含 examples + edge cases + limits）─────

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List files and subdirectories in a directory. Much faster than using run_python for ls/os.listdir(). "
                "Use this first to explore project structure before reading files. "
                "Example: list_directory('.') shows project root. list_directory('Skills', '*.md') shows all SKILL.md files. "
                "Returns: dirs[], files[] with name and size_bytes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (relative to project root or absolute). Default: '.' (project root)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional glob pattern to filter entries, e.g. '*.md', '**/*.py'",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_file",
            "description": (
                "Search for a pattern inside a file and return matching lines with context. "
                "Use this instead of read_file when you need to find specific content (e.g. 'Total Debit', 'Summary', column headers). "
                "Saves 90%+ context tokens vs reading the full file. Supports regex and plain string patterns (case-insensitive). "
                "Example: search_file('extracted.txt', 'total debit', context_lines=3) "
                "Returns: match_count, list of {line_number, matched_line, context}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to search (relative to project root or absolute)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "String or regex pattern to search for (case-insensitive)",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Lines of context to show above/below each match (default: 2)",
                    },
                    "max_matches": {
                        "type": "integer",
                        "description": "Maximum matches to return (default: 20)",
                    },
                },
                "required": ["path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from disk. Supports partial reads to avoid large context bloat. "
                "For large files, use start_line/end_line or max_chars to read only what you need. "
                "Returns: content, total_lines, total_chars, path. "
                "Examples: "
                "read_file('Skills/xlsx/SKILL.md') — read a skill description. "
                "read_file('output.txt', start_line=1, end_line=50) — read first 50 lines. "
                "read_file('big_file.txt', max_chars=3000) — read first 3000 chars only. "
                "TIP: Use list_directory first to check file sizes, then read_file with limits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path (relative to project root or absolute)",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to read (1-indexed, inclusive). Omit to start from beginning.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to read (1-indexed, inclusive). Omit to read to end.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return (truncates at this limit). Use for large files.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a file (OVERWRITES existing content, creates parent dirs if needed). "
                "Use for creating new Python scripts, saving intermediate results, or writing the final Excel generation script. "
                "Returns: success, path, bytes_written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path to write"},
                    "content": {"type": "string", "description": "Full text content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace the FIRST occurrence of old_string with new_string in a file. "
                "Fails with an error (and file preview) if old_string is not found — does NOT silently skip. "
                "Use for targeted fixes: change one function, fix one variable, etc. "
                "Returns: success, replaced=1 on success. "
                "TIP: If you need to replace multiple occurrences, call edit_file multiple times."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact string to find and replace (must exist in file)"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python code in a sandboxed subprocess. Returns stdout, stderr, exit_code. "
                "TIMEOUT: 60 seconds — if your code may take longer, split into smaller steps. "
                "AVAILABLE LIBRARIES: pdfplumber, openpyxl, pandas, tabula, PyMuPDF (fitz), re, json, os, pathlib. "
                "IMPORTANT — keep stdout concise to avoid context bloat: "
                "  ✓ print(df.head(10)) instead of print(df) "
                "  ✓ print(f'Found {len(rows)} rows, debit total={total:.2f}') "
                "  ✗ print(df.to_string()) on a 200-row DataFrame "
                "State persists only within one call — files written to disk ARE accessible in later calls. "
                "Example: extract PDF to a text file first, then process that file in a separate call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                    "working_dir": {
                        "type": "string",
                        "description": "Working directory for the script (default: project root)",
                    },
                },
                "required": ["code"],
            },
        },
    },
]


def execute(
    pdf_path: str,
    output_path: str,
    plan: dict,
    skills_catalog_text: str,
    feedback: str = None,
    prev_summary: dict = None,
) -> dict:
    """
    Generator Agent (mimo-v2.5, reasoning=medium).
    Anthropic Plan A: fresh context per iteration.
    """
    project_root = str(Path(__file__).parent)
    scratch_path = str(Path(__file__).parent / "scratch.md")

    system_prompt = f"""You are a Generator Agent in a Harness Engineering system.
You autonomously decide HOW to accomplish the task. Choose your own approach, libraries, and sequence.

## Tools available
- list_directory(path, pattern): explore directory structure — use this FIRST before reading files
- search_file(path, pattern, context_lines, max_matches): find specific content in a file efficiently
- read_file(path, start_line, end_line, max_chars): read files with optional range limits
- write_file(path, content): write/overwrite a file
- edit_file(path, old_string, new_string): targeted single-occurrence replacement
- run_python(code, working_dir): execute Python (60s timeout, pdfplumber/openpyxl/pandas available)

## Project root
{project_root}

## Skills
The Skills/ directory contains SKILL.md files describing available utilities.
Use list_directory('Skills') to discover them, then read_file to understand each one.
Do NOT try to guess — read the SKILL.md files before using any skill.

## Scratchpad
Write key findings to {scratch_path} so you don't lose them if context is cleared.
Example: PDF column names, transaction count, payee list, Summary section values.
Update the scratchpad as you discover new information.

## Stdout discipline (critical for context efficiency)
In run_python code: print summaries, not full DataFrames.
  ✓ print(f"Extracted {{len(rows)}} rows. Debit total={{debit_total:.2f}}")
  ✗ print(df.to_string())  # never do this

## Acceptance Criteria (WHAT must be true when you finish):
1. The PDF's Summary/Balance section total Debit and Credit are extracted for verification
2. All individual transactions are extracted from the PDF
3. Each transaction has a specific named Payee/Buyer (not "Unknown")
4. Excel saved to: {output_path}
   Sheet: "By Payee & Buyer" (ONE sheet only)
   - Transactions grouped by Payee/Buyer
   - Each group: rows with Date, Description, Ref/Inv No., Debit, Credit
   - Each group ends with a Subtotal row showing sum of Debit and Credit for that group
5. Sum of all extracted transaction Debits and Credits verified against Summary totals
6. **MANDATORY — your very last run_python call must print this marker line** (after saving Excel):
   ###SUMMARY### {{"status":"completed","output_path":"{output_path}","transaction_count":<N>,"payee_count":<N>,"summary_debit":<float|null>,"summary_credit":<float|null>,"extracted_debit":<float>,"extracted_credit":<float>}}
   - summary_debit/summary_credit: use null if PDF has no Summary section (do NOT omit the fields)"""

    plan_str = json.dumps(plan, indent=2, ensure_ascii=False)

    # ── Anthropic Plan A: fresh context per iteration ─────────────────────────
    if not feedback:
        user_message = f"""Process this bank statement PDF and produce the Excel output.

**PDF**: {pdf_path}
**Output Excel**: {output_path}

**Planner's analysis** (structural context — use as reference, not prescribed steps):
{plan_str}

**Skills discovery**: Run list_directory('Skills') to find available SKILL.md files, then read them as needed.

Start by exploring the project structure and reading the relevant SKILL.md files before extracting data."""

    else:
        prev_info = ""
        if prev_summary:
            prev_info = f"""
**Previous iteration artifact** (what was already achieved):
- Transactions extracted: {prev_summary.get('transaction_count', 'unknown')}
- Payees identified: {prev_summary.get('payee_count', 'unknown')}
- Summary Debit (from PDF): {prev_summary.get('summary_debit', 'N/A')}
- Summary Credit (from PDF): {prev_summary.get('summary_credit', 'N/A')}
- Extracted Debit (sum of rows): {prev_summary.get('extracted_debit', 'N/A')}
- Extracted Credit (sum of rows): {prev_summary.get('extracted_credit', 'N/A')}
- Output file exists: {Path(output_path).exists()}
"""
        user_message = f"""The previous iteration produced output that failed the Evaluator's quality check.
Fix the specific issues below and regenerate the Excel output.

**PDF**: {pdf_path}
**Output Excel**: {output_path}
{prev_info}
**Planner's structural context**:
{plan_str}

**⚠️ EVALUATOR FEEDBACK — exact issues to fix**:
{feedback}

Check the scratchpad first ({scratch_path}) — it may contain findings from the previous run.
Focus on fixing these issues. Re-read the PDF only if needed."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_message},
    ]

    tool_call_count = 0
    final_summary   = {}

    # Repeated-failure detection: track (tool_name, args_hash) → consecutive failures
    failure_tracker: dict[str, int] = {}

    def _safe_truncate(result: dict, max_chars: int = 6000) -> str:
        """Ingestion-time truncation: truncate content fields BEFORE json.dumps."""
        if isinstance(result, dict):
            for key in ("stdout", "content", "stderr"):
                if key in result and isinstance(result[key], str) and len(result[key]) > max_chars:
                    result[key] = result[key][:max_chars] + f"\n...[truncated — {len(result[key])} chars total]"
        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized) > max_chars + 500:
            serialized = json.dumps({"truncated": True, "preview": serialized[:max_chars]}, ensure_ascii=False)
        return serialized

    def _run_tool(tc):
        try:
            t_input = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError) as e:
            err = {"success": False, "error": f"Failed to parse tool arguments: {e}",
                   "raw_args_preview": (tc.function.arguments or "")[:200]}
            return tc.id, tc.function.name, {}, json.dumps(err), False
        result = tool_module.dispatch(tc.function.name, t_input)
        success = result.get("success", True)
        return tc.id, tc.function.name, t_input, _safe_truncate(result), success

    for _round in range(MAX_TOOL_ROUNDS):
        response = llm_client.chat(
            messages=messages,
            model=llm_client.EXECUTOR_MODEL,
            reasoning="medium",
            max_tokens=8192,
            tools=OPENAI_TOOLS,
            stream=True,
            stream_label=f"[Generator round {_round+1}]",
        )

        choice = response.choices[0]
        msg    = choice.message

        # Serialize tool_calls for message history
        tool_calls_dict = None
        if msg.tool_calls:
            tool_calls_dict = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": tool_calls_dict})

        # Check completion
        if choice.finish_reason == "stop" or not msg.tool_calls:
            text = msg.content or ""
            # Robust summary extraction via ###SUMMARY### marker
            marker_match = re.search(r"###SUMMARY###\s*(\{.+\})", text)
            if marker_match:
                try:
                    final_summary = json.loads(marker_match.group(1))
                except Exception:
                    pass
            # Fallback: look in previous tool stdout results
            if not final_summary:
                for m in reversed(messages):
                    if m.get("role") == "tool":
                        try:
                            tool_data = json.loads(m["content"])
                            stdout = tool_data.get("stdout", "")
                            marker_match = re.search(r"###SUMMARY###\s*(\{.+\})", stdout)
                            if marker_match:
                                final_summary = json.loads(marker_match.group(1))
                                break
                        except Exception:
                            continue
            break

        # ── Parallel strategy: read-only tools parallel, run_python serial ───
        # Anthropic: "be careful with stateful side effects in parallel execution"
        readonly_calls  = [tc for tc in msg.tool_calls if tc.function.name in ("list_directory", "search_file", "read_file")]
        stateful_calls  = [tc for tc in msg.tool_calls if tc.function.name not in ("list_directory", "search_file", "read_file")]

        results_map = {}

        # Read-only: parallel (safe, no side effects)
        if readonly_calls:
            with ThreadPoolExecutor(max_workers=min(len(readonly_calls), 4)) as pool:
                futures = {pool.submit(_run_tool, tc): tc for tc in readonly_calls}
                for future in as_completed(futures):
                    tc_id, t_name, t_input, result_str, success = future.result()
                    tool_call_count += 1
                    _log_tool(tool_call_count, t_name, t_input, success)
                    results_map[tc_id] = (result_str, success, t_name, t_input)

        # Stateful (write_file, edit_file, run_python): serial to prevent race conditions
        for tc in stateful_calls:
            tc_id, t_name, t_input, result_str, success = _run_tool(tc)
            tool_call_count += 1
            _log_tool(tool_call_count, t_name, t_input, success)
            results_map[tc.id] = (result_str, success, t_name, t_input)

            # Repeated-failure detection (Anthropic: "pause at checkpoints when stuck")
            fail_key = f"{t_name}:{hash(tc.function.arguments)}"
            if not success:
                failure_tracker[fail_key] = failure_tracker.get(fail_key, 0) + 1
                if failure_tracker[fail_key] >= MAX_CONSECUTIVE_FAILURES:
                    print(f"\n  \033[31m[Generator] Tool '{t_name}' failed {MAX_CONSECUTIVE_FAILURES} times with same args — stopping to prevent infinite loop\033[0m")
                    # Inject a hard stop message
                    messages.extend([
                        {"role": "tool", "tool_call_id": tc.id, "content": result_str}
                        for tc in stateful_calls if tc.id not in results_map or tc.id == tc_id
                    ])
                    messages.append({
                        "role": "user",
                        "content": f"SYSTEM: Tool '{t_name}' has failed {MAX_CONSECUTIVE_FAILURES} consecutive times with the same arguments. Try a completely different approach or use a different tool.",
                    })
                    failure_tracker.clear()
                    break
            else:
                failure_tracker.pop(fail_key, None)

        # Add tool results in original call order
        tool_results_messages = []
        for tc in msg.tool_calls:
            if tc.id in results_map:
                result_str = results_map[tc.id][0]
            else:
                result_str = json.dumps({"success": False, "error": "Tool result not available"})
            tool_results_messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})
        messages.extend(tool_results_messages)

        # ── Context clearing (Anthropic Tool Result Clearing) ─────────────────
        # Trigger at 50K tokens (earlier = safer), keep last 5 tool results
        estimated_tokens = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4
        if estimated_tokens > CONTEXT_CLEAR_THRESHOLD:
            tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
            to_clear = tool_indices[:-KEEP_LAST_TOOL_RESULTS] if len(tool_indices) > KEEP_LAST_TOOL_RESULTS else []
            for i in to_clear:
                messages[i]["content"] = "[cleared — see scratch.md for key findings]"
            if to_clear:
                print(f"  \033[2m[Context] ~{estimated_tokens:,} tokens — cleared {len(to_clear)} old tool results\033[0m")

    output_exists = Path(output_path).exists()
    return {
        "success": output_exists,
        "output_path": output_path,
        "tool_calls": tool_call_count,
        "summary": final_summary,
        "error": None if output_exists else "Output Excel file was not created",
    }


def _log_tool(count: int, name: str, t_input: dict, success: bool):
    """Clean tool call log line."""
    status = "\033[32m✓\033[0m" if success else "\033[31m✗\033[0m"
    preview = str(t_input)[:80]
    print(f"  {status} [Tool #{count}] {name}: {preview}...", flush=True)


if __name__ == "__main__":
    import sys
    import skill_loader

    if len(sys.argv) < 3:
        print("Usage: python executor.py <pdf_path> <output_path>")
        sys.exit(1)

    catalog = skill_loader.load()
    skills_text = skill_loader.format_for_prompt(catalog)
    mock_plan = {
        "extraction_approach": "pdfplumber tables then text fallback",
        "payee_buyer_strategy": "Extract from Description or dedicated Payee column",
    }
    result = execute(sys.argv[1], sys.argv[2], mock_plan, skills_text)
    print(json.dumps(result, indent=2, ensure_ascii=False))
