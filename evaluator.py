"""
Evaluator Agent — Agentic Quality Verifier (2026)

Architecture: LLM + tools verifies everything. Python only gates on file existence.
  Tier 1 (Python): file existence gate only
  Tier 2 (LLM+tools): discovers Excel structure, verifies all 5 criteria including ground_truth
  Tier 3 (Python): fallback if LLM fails to emit verdict, compute passed
"""

import json
import re
from pathlib import Path
import openpyxl
import tools as tool_module
import llm_client


MAX_EVAL_ROUNDS = 12

# ── Evaluator tools: read + run only ──────────────────────────────────────────

EVAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python code in a sandboxed subprocess. Returns stdout, stderr, exit_code. "
                "Use to read and verify the Excel file with pandas/openpyxl. "
                "AVAILABLE LIBRARIES: openpyxl, pandas, re, json, os, pathlib. "
                "Keep stdout concise — print counts and summaries, not full DataFrames. "
                "Timeout: 60 seconds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from disk. Supports start_line/end_line/max_chars to avoid large context bloat. "
                "Returns: content, total_lines, total_chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to project root or absolute)"},
                    "start_line": {"type": "integer", "description": "First line to read (1-indexed)"},
                    "end_line": {"type": "integer", "description": "Last line to read (1-indexed)"},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return"},
                },
                "required": ["path"],
            },
        },
    },
]


# ── TIER 1: file existence gate only ──────────────────────────────────────────

def _check_excel_exists(output_path: str) -> dict:
    p = Path(output_path)
    if not p.exists():
        return {"ok": False, "reason": "File does not exist"}
    try:
        wb = openpyxl.load_workbook(output_path)
        return {"ok": True, "sheets": wb.sheetnames}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ── TIER 2: Agentic LLM verifier ──────────────────────────────────────────────

def evaluate(output_path: str, pdf_path: str, gen_summary: dict = None,
             log_dir: str = None, iteration: int = 1) -> dict:
    """
    Agentic Evaluator:
      Tier 1 (Python): file exists gate only
      Tier 2 (LLM+tools): discovers structure, verifies all 5 criteria
      Tier 3 (Python): fallback verdict + compute passed
    """

    # ── Tier 1 gate ───────────────────────────────────────────────────────────
    file_check = _check_excel_exists(output_path)
    if not file_check["ok"]:
        return {
            "score": 0.0,
            "passed": False,
            "feedback": f"Excel file not created: {file_check.get('reason')}",
            "checks": {"file_exists": file_check},
        }

    print("  \033[2m[Evaluator] Tier 2 — LLM agentic verification starting...\033[0m", flush=True)

    gen_summary_str = json.dumps(gen_summary, ensure_ascii=False) if gen_summary else "NOT PROVIDED"

    system_prompt = """You are an independent Evaluator Agent in a bank statement extraction pipeline.
You did NOT generate this Excel file. Your job: discover its actual structure, then score it against all acceptance criteria.

<acceptance_criteria>
These criteria are FUNCTIONAL — they describe what the output must achieve, not how it must look.
The Generator may use any layout, sheet name, or column arrangement. Your job is to adapt.

AC1. data_sheet (0-2):
  Score 2: There is a sheet containing grouped transaction data with 10+ transaction rows
  Score 1: A sheet exists but has fewer than 5 meaningful transaction rows
  Score 0: No sheet with transaction data found

AC2. transaction_data (0-2):
  Score 2: Transaction rows have real dates, non-empty descriptions, and numeric Debit or Credit amounts
  Score 1: Most rows are real but some have blank dates, empty descriptions, or zero amounts
  Score 0: Rows are empty, all-zero, or contain placeholder data

AC3. payee_identification (0-2):
  Score 2: Every group is labeled with a specific named entity (company or person name). Zero "Unknown" labels.
  Score 1: Most groups are identified; some are "Unknown" or vague
  Score 0: Majority of groups are "Unknown" or unlabeled

AC4. grouping_subtotals (0-2):
  Each group of transactions must have a summary/total row whose Debit and Credit match the group sum. Tolerance RM 1.00.
  Score 2: All groups have correct totals
  Score 1: Less than 30% of groups have total errors
  Score 0: 30%+ of groups have errors, or no total rows found at all

AC5. ground_truth_match (0-2):
  Compare the Generator's reported totals against the PDF summary.
  From the Generator summary report: summary_debit, summary_credit, extracted_debit, extracted_credit.
  Score 2: summary_debit and extracted_debit match within RM 1.00 (and credit if present)
  Score 1: PDF has no Summary section (summary_debit is null) — Generator tried, acceptable
  Score 0: Totals mismatch by more than RM 1.00, OR gen_summary was NOT PROVIDED

AC6. raw_transactions_sheet (0-2):
  Score 2: A second sheet exists (e.g. "Transactions") with a Page column containing integers and all raw transactions in PDF order. Row count matches the grouped transaction count in Sheet 1.
  Score 1: Sheet exists but Page column is missing or row count is significantly different from Sheet 1.
  Score 0: No raw transactions sheet found.
</acceptance_criteria>

<instructions>
Your goal: independently verify the Excel file against all 6 acceptance criteria and produce an evidence-based score (0–12).

Discover the actual file structure before evaluating — do not assume column names, sheet names, or layout. Use any approach and as many run_python calls as you need.

Key technical constraint for AC4: load with data_only=False so formula strings are visible. Subtotal cells may contain floats (use directly), formula strings starting with '=' (parse and sum the referenced range), or None (mismatch). Tolerance is RM 1.00.

When you have enough evidence for all 6 criteria, emit this as your final line:
###VERDICT### {{"score": <total 0-12>, "criteria": {{"data_sheet": <0-2>, "transaction_data": <0-2>, "payee_identification": <0-2>, "grouping_subtotals": <0-2>, "ground_truth_match": <0-2>, "raw_transactions_sheet": <0-2>}}, "strengths": ["<max 80 chars>"], "issues": ["<max 80 chars>"], "feedback": "<max 300 chars — the single most important fix>"}}

No text after ###VERDICT###.
</instructions>

<discovery_discipline>
Never hardcode assumptions about sheet names, column names, or total-row keywords. Read the file to discover the actual structure. If the Generator used "Jumlah" instead of "Subtotal", detect it from the file and use it consistently.
</discovery_discipline>

<scoring_discipline>
Every score must cite what your code returned — counts, examples, specific rows. Ambiguous or missing evidence → score lower, not higher.
</scoring_discipline>

<output_discipline>
Print counts and short summaries only — a single summary line per verification step is enough.
</output_discipline>

<evaluator_boundary>
You are an evaluator, not a fixer. Never write to, modify, or patch the Excel file. If something is wrong, reflect it in the score and feedback — the Generator will fix it in the next iteration.
</evaluator_boundary>"""

    user_message = f"""Evaluate this Excel output against all 6 acceptance criteria.

<context>
Excel file: {output_path}
Source PDF: {pdf_path}
Generator summary report: {gen_summary_str}
</context>

Emit ###VERDICT### as your final line."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_message},
    ]

    tool_call_count = 0
    verdict = None

    def _safe_truncate(result: dict, max_chars: int = 4000) -> str:
        if isinstance(result, dict):
            for key in ("stdout", "content", "stderr"):
                if key in result and isinstance(result[key], str) and len(result[key]) > max_chars:
                    result[key] = result[key][:max_chars] + "\n...[truncated]"
        return json.dumps(result, ensure_ascii=False)

    for _round in range(MAX_EVAL_ROUNDS):
        response = llm_client.chat(
            messages=messages,
            model=llm_client.EVALUATOR_MODEL,
            reasoning="medium",
            max_tokens=8192,
            tools=EVAL_TOOLS,
            stream=True,
            stream_label="[Evaluator]",
        )

        choice = response.choices[0]
        msg    = choice.message

        # Check for ###VERDICT### in assistant text
        text = msg.content or ""
        verdict_match = re.search(r"###VERDICT###\s*(\{.+\})", text, re.DOTALL)
        if verdict_match:
            try:
                verdict = json.loads(verdict_match.group(1))
            except Exception:
                pass

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

        # Stop if verdict found or no more tool calls
        if verdict or choice.finish_reason == "stop" or not msg.tool_calls:
            if not verdict:
                for m in reversed(messages[-10:]):
                    if m.get("role") == "tool":
                        try:
                            tool_data = json.loads(m["content"])
                            stdout = tool_data.get("stdout", "")
                            vm = re.search(r"###VERDICT###\s*(\{.+\})", stdout, re.DOTALL)
                            if vm:
                                verdict = json.loads(vm.group(1))
                                break
                        except Exception:
                            continue
            break

        # Execute tool calls sequentially
        tool_results = []
        for tc in msg.tool_calls:
            try:
                t_input = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError) as e:
                result_str = json.dumps({"success": False, "error": f"Bad tool arguments: {e}"})
                tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})
                continue

            result = tool_module.dispatch(tc.function.name, t_input)
            success = result.get("success", True)
            tool_call_count += 1

            status  = "\033[32m✓\033[0m" if success else "\033[31m✗\033[0m"
            preview = str(t_input)[:80]
            print(f"  {status} [Eval #{tool_call_count}] {tc.function.name}: {preview}...", flush=True)

            tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": _safe_truncate(result)})

        messages.extend(tool_results)

    # ── Tier 3: fallback + compute passed ────────────────────────────────────
    if not verdict:
        verdict = {
            "score": 0.0,
            "criteria": {},
            "feedback": "Evaluator did not emit a ###VERDICT### — check logs",
            "strengths": [],
            "issues": ["Evaluator failed to complete verification"],
        }

    criteria = verdict.get("criteria", {})
    score = float(sum(criteria.get(k, 0) for k in [
        "data_sheet", "transaction_data", "payee_identification",
        "grouping_subtotals", "ground_truth_match", "raw_transactions_sheet",
    ]))

    print(f"  \033[2m[Evaluator] done — {tool_call_count} tool call(s), score={score}/12\033[0m", flush=True)

    if log_dir:
        llm_client.save_conversation_log(
            messages=messages,
            agent="Evaluator",
            iteration=iteration,
            model=llm_client.EVALUATOR_MODEL,
            log_dir=log_dir,
            extra={"score": score, "criteria": criteria, "verdict": verdict},
        )

    return {
        "score": score,
        "passed": score >= 12.0,
        "feedback": verdict.get("feedback", ""),
        "criteria": criteria,
        "strengths": verdict.get("strengths", []),
        "issues": verdict.get("issues", []),
        "checks": {"file_exists": file_check},
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python evaluator.py <output_excel> <source_pdf>")
        sys.exit(1)
    result = evaluate(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2, ensure_ascii=False))
