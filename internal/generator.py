"""
Generator Agent — Anthropic 官方最佳实践 (2026)

架构：单 Agent 负责规划 + 执行（Anthropic 推荐做法）
- 合并了 Planner 的 PDF 分析职责（由 reasoning=high extended thinking 替代）
- 遵循 Anthropic 官方 XML 标签 System Prompt 规范
- 保留所有 ACI 工具原则、并行策略、Scratchpad、失败检测

改进点（对照 Anthropic 官方文档）：
1. 移除 plan/skills_catalog_text 参数 — Generator 自主探索，无需 Planner
2. 模型升级至 mimo-v2.5-pro + reasoning=high — 支持内部规划
3. System Prompt 重写为 XML 结构 — Anthropic 官方规范
4. 工具描述含 examples / edge cases — "put yourself in the model's shoes"
5. 只读工具并行，stateful 工具串行 — 防止并发写入竞争
6. Scratchpad (scratch.md) — 外部持久记忆，context 清除后不失忆
7. ###SUMMARY### marker — 精确解析 JSON summary
8. 重复失败检测 — 同工具同参数连续失败 3 次则注入 SYSTEM 消息
"""

import json
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from . import tools as tool_module
from . import llm_client


MAX_TOOL_ROUNDS = 30
CONTEXT_CLEAR_THRESHOLD = 50_000
KEEP_LAST_TOOL_RESULTS  = 5
MAX_CONSECUTIVE_FAILURES = 3

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


def generate(
    pdf_path: str,
    output_path: str,
    skills_catalog_text: str = None,
    feedback: str = None,
    prev_summary: dict = None,
    log_dir: str = None,
    iteration: int = 1,
    sandbox_dir: str = None,
) -> dict:
    """
    Generator Agent (mimo-v2.5-pro, reasoning=high).
    Plans internally via extended thinking, then executes autonomously.
    Anthropic Plan A: fresh context per iteration.
    skills_catalog_text: Progressive Disclosure Layer 1 — name+description only.
    sandbox_dir: per-task isolated workspace (overrides default scratch.md location).
    """
    project_root = str(Path(__file__).parent)
    if sandbox_dir:
        scratch_path = str(Path(sandbox_dir) / "scratch.md")
    else:
        scratch_path = str(Path(__file__).parent / "scratch.md")

    # ── Skills section: Anthropic Progressive Disclosure Layer 1 ─────────────
    skills_section = ""
    if skills_catalog_text:
        skills_section = f"""
<skills>
The following skills are available. Each entry is: skill-name: one-line description. Read the full SKILL.md only when you need detailed instructions (Progressive Disclosure — load on demand, not upfront).

{skills_catalog_text}
</skills>
"""

    # ── Anthropic XML-structured System Prompt ────────────────────────────────
    system_prompt = f"""You are a bank statement extraction agent. You read a PDF and produce a verified Excel output.
{skills_section}
<instructions>
Your goal: extract all transactions from the PDF and produce an Excel output that meets every acceptance criterion in the user message.

Use your extended thinking to plan before acting. When you have enough information to act, act — do not re-derive facts already established. Commit to your approach once chosen.

If you intend to call multiple tools and there are no dependencies between them, make all the independent calls in parallel. Only call sequentially when the output of one call is needed as input to the next.

When your output is complete and verified, emit this as the last stdout line of your final run_python call:
###SUMMARY### {{"status":"completed","output_path":"<output path from context>","transaction_count":<N>,"payee_count":<N>,"summary_debit":<float|null>,"summary_credit":<float|null>,"extracted_debit":<float>,"extracted_credit":<float>}}
Use null for summary_debit/summary_credit if the PDF has no Summary section.
</instructions>

<output_format>
Model the "By Payee & Buyer" sheet exactly after this layout:

SHEET HEADER (rows 1–2):
  Row 1: Company / bank statement title (merged across columns)
  Row 2: Bank name and account number
  Row 3: (blank)

SECTION 1 — BUYERS / PAYERS  (Credit transactions — money received):
  Row: ▶  SECTION 1 :  BUYERS / PAYERS
  Row: No. | Date | Month | Description | Ref / Inv No. | Credit (RM)
  For each Buyer group:
    Payee header row: "  [Full Payee Name]  (N transactions)"  in col A, total credit in Credit col
    Transaction rows: sequential No. | DD/MM/YYYY | Mon-YY | Description | Ref | amount
    Subtotal row: "Subtotal — [Full Payee Name]" in col A, subtotal amount in Credit col
  After all groups: "TOTAL RECEIPTS (CREDITS)" row with grand total

SECTION 2 — PAYEES  (Debit transactions — money paid out):
  (blank row separator)
  Row: ▶  SECTION 2 :  PAYEES  (PAYMENTS)
  Row: No. | Date | Month | Description | Ref / Inv No. | Debit (RM)
  For each Payee group (same structure as above but Debit col):
    Payee header row: "  [Full Payee Name]  (N transactions)"  in col A, total debit in Debit col
    Transaction rows: sequential No. | DD/MM/YYYY | Mon-YY | Description | Ref | amount
    Subtotal row: "Subtotal — [Full Payee Name]" in col A, subtotal in Debit col
  After all groups: "TOTAL PAYMENTS (DEBITS)" row with grand total

COLUMN ORDER (7 columns):
  A: No.  |  B: Date  |  C: Month  |  D: Description  |  E: Ref / Inv No.  |  F: Debit (RM)  |  G: Credit (RM)

KEY RULES:
- Payee header rows: name indented with 2 leading spaces, transaction count in parentheses, total right-aligned in amount column
- Month format: "Oct-25" (Mon-YY abbreviation)
- Date format: DD/MM/YYYY
- Transaction numbering: sequential integers across the entire sheet (not restarting per group)
- Subtotal rows: "Subtotal — [Full Payee Name]" — never truncate the name
- Section/total rows span visually across the row; amounts in the correct Debit or Credit column
- Transactions with Credit amount → SECTION 1 only; transactions with Debit amount → SECTION 2 only
</output_format>

<scratchpad_discipline>
The scratchpad (path given in context) is your external persistent memory. Context is cleared between iterations, but the scratchpad persists across runs. Write key findings there (transaction count, payee mappings, PDF structure, column indices) so you don't re-derive them on feedback rounds.
</scratchpad_discipline>

<output_discipline>
Every byte of stdout gets stored in the conversation and re-sent on every subsequent LLM call. Minimize context by printing only counts, totals, summary statistics, and very short samples (3-5 items max).

Example: print(f"Extracted {{len(rows)}} rows. Debit={{debit:.2f}}, Credit={{credit:.2f}}. Sample payees: {{list(payees)[:3]}}")

Emit ###SUMMARY### only in your very last run_python call — earlier calls must not contain it.
</output_discipline>

<extraction_quality>
Transactions span all PDF pages — extract from every page and verify your extracted total matches the PDF's own Summary/Balance section. A mismatch means missed transactions.

Every payee name must be a recognisable, complete entity. Bank PDF columns are narrow and names may be truncated in the source — reconstruct the full name from adjacent cells or context. If a payee truly cannot be determined, write "REVIEW NEEDED (Page N)".
</extraction_quality>

<technical_constraints>
Write Python-computed float values into subtotal cells. openpyxl-generated files have no formula cache — any reader calling data_only=True gets None for formula cells, which breaks downstream verification.
</technical_constraints>

<iteration_behavior>
When evaluator feedback is provided, read the scratchpad first — it contains findings from the previous run that may save re-reading the PDF entirely.

When the Excel file already exists and has a specific issue, patch only what's broken — rebuilding from scratch for a minor fix wastes tool calls.
</iteration_behavior>"""

    # ── Anthropic Plan A: fresh context per iteration ─────────────────────────
    # Dynamic content (paths, task criteria) lives in user message; system prompt stays frozen.
    context_block = f"""<context>
PDF input:    {pdf_path}
Output Excel: {output_path}
Scratchpad:   {scratch_path}
Project root: {project_root}
</context>"""

    acceptance_criteria_block = """<acceptance_criteria>
1. The Excel file exists at the output path and is a valid .xlsx file.
2. Contains two sheets: "By Payee & Buyer" (grouped with subtotals) and "Transactions" (raw).
3. Every transaction row has: non-empty Date, non-empty Description, specific named Payee (no "Unknown"), and at least one of Debit or Credit as a numeric value.
4. Every payee group ends with a Subtotal row whose Debit and Credit match the group sum within RM 1.00.
5. Sum of all extracted Debit values matches PDF Summary section Total Debit within RM 1.00. If PDF has no Summary section, use summary_debit: null in ###SUMMARY###.
6. ###SUMMARY### marker emitted as the last stdout line of your last run_python call.
7. Sheet "Transactions" columns: Page | Date | Description | Ref/Inv No. | Debit | Credit. All transactions in original PDF order. Page = PDF page number (integer, 1-based). Row count must equal transaction_count in ###SUMMARY###.
</acceptance_criteria>"""

    if not feedback:
        user_message = f"""Process this bank statement PDF and produce the Excel output.

{context_block}

{acceptance_criteria_block}"""

    else:
        prev_info = ""
        if prev_summary:
            prev_info = f"""
Previous iteration results:
- Transactions extracted: {prev_summary.get('transaction_count', 'unknown')}
- Payees identified:      {prev_summary.get('payee_count', 'unknown')}
- Summary Debit (PDF):    {prev_summary.get('summary_debit', 'N/A')}
- Summary Credit (PDF):   {prev_summary.get('summary_credit', 'N/A')}
- Extracted Debit (rows): {prev_summary.get('extracted_debit', 'N/A')}
- Extracted Credit (rows):{prev_summary.get('extracted_credit', 'N/A')}
- Output file exists:     {Path(output_path).exists()}
"""
        user_message = f"""The previous iteration failed the Evaluator's quality check. Fix the specific issues below.

{context_block}
{prev_info}
{acceptance_criteria_block}

Read the scratchpad first — it contains findings from the previous run that will save time.

EVALUATOR FEEDBACK — exact issues to fix:
{feedback}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_message},
    ]

    tool_call_count = 0
    final_summary   = {}
    failure_tracker: dict[str, int] = {}

    def _safe_truncate(result: dict, max_chars: int = 6000) -> str:
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
            model=llm_client.GENERATOR_MODEL,
            reasoning="medium",
            max_tokens=8192,
            tools=OPENAI_TOOLS,
            stream=True,
            stream_label=f"[Generator round {_round+1}]",
        )

        choice = response.choices[0]
        msg    = choice.message

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
            marker_match = re.search(r"###SUMMARY###\s*(\{.+\})", text)
            if marker_match:
                try:
                    final_summary = json.loads(marker_match.group(1))
                except Exception:
                    pass
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

        # ── Parallel strategy: read-only tools parallel, stateful serial ──────
        readonly_calls = [tc for tc in msg.tool_calls if tc.function.name in ("list_directory", "search_file", "read_file")]
        stateful_calls = [tc for tc in msg.tool_calls if tc.function.name not in ("list_directory", "search_file", "read_file")]

        results_map = {}

        if readonly_calls:
            with ThreadPoolExecutor(max_workers=min(len(readonly_calls), 4)) as pool:
                futures = {pool.submit(_run_tool, tc): tc for tc in readonly_calls}
                for future in as_completed(futures):
                    tc_id, t_name, t_input, result_str, success = future.result()
                    tool_call_count += 1
                    _log_tool(tool_call_count, t_name, t_input, success)
                    results_map[tc_id] = (result_str, success, t_name, t_input)

        for tc in stateful_calls:
            tc_id, t_name, t_input, result_str, success = _run_tool(tc)
            tool_call_count += 1
            _log_tool(tool_call_count, t_name, t_input, success)
            results_map[tc.id] = (result_str, success, t_name, t_input)

            fail_key = f"{t_name}:{hash(tc.function.arguments)}"
            if not success:
                failure_tracker[fail_key] = failure_tracker.get(fail_key, 0) + 1
                if failure_tracker[fail_key] >= MAX_CONSECUTIVE_FAILURES:
                    print(f"\n  \033[31m[Generator] Tool '{t_name}' failed {MAX_CONSECUTIVE_FAILURES} times with same args — stopping to prevent infinite loop\033[0m")
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
        estimated_tokens = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4
        if estimated_tokens > CONTEXT_CLEAR_THRESHOLD:
            tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
            to_clear = tool_indices[:-KEEP_LAST_TOOL_RESULTS] if len(tool_indices) > KEEP_LAST_TOOL_RESULTS else []
            for i in to_clear:
                messages[i]["content"] = "[cleared — see scratch.md for key findings]"
            if to_clear:
                print(f"  \033[2m[Context] ~{estimated_tokens:,} tokens — cleared {len(to_clear)} old tool results\033[0m")

    output_exists = Path(output_path).exists()

    if log_dir:
        llm_client.save_conversation_log(
            messages=messages,
            agent="Generator",
            iteration=iteration,
            model=llm_client.GENERATOR_MODEL,
            log_dir=log_dir,
            extra={"tool_calls": tool_call_count, "summary": final_summary},
        )

    return {
        "success": output_exists,
        "output_path": output_path,
        "tool_calls": tool_call_count,
        "summary": final_summary,
        "error": None if output_exists else "Output Excel file was not created",
    }


def _log_tool(count: int, name: str, t_input: dict, success: bool):
    status = "\033[32m✓\033[0m" if success else "\033[31m✗\033[0m"
    preview = str(t_input)[:80]
    print(f"  {status} [Tool #{count}] {name}: {preview}...", flush=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python generator.py <pdf_path> <output_path>")
        sys.exit(1)

    result = generate(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2, ensure_ascii=False))
