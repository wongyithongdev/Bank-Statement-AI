"""
DEPRECATED — 2026-06-17
planner.py is no longer called by main.py.
PDF structure analysis is now handled by generator.py via extended reasoning (reasoning=high).
This file is kept for reference only. Do not import or call plan().

Original: Planner Agent — analyzed PDF and available Skills, generated execution plan.
Model: mimo-v2.5-pro | reasoning: high
"""

import json
import pdfplumber
from pathlib import Path
from . import llm_client


def _extract_pdf_preview(pdf_path: str, head_pages: int = 3, tail_pages: int = 2) -> str:
    """
    提取 PDF 前几页 + 最后几页 作为 Planner 的上下文。
    前几页：了解结构、列格式、账户信息。
    最后几页：找 Summary/Balance section（银行对账单的汇总通常在最后页）。
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)

            def _extract_page(page, label):
                text = page.extract_text() or ""
                tables = page.extract_tables()
                table_text = ""
                if tables:
                    for t in tables[:2]:
                        rows = [" | ".join(str(c) for c in row) for row in t if row]
                        table_text += "\n[TABLE]\n" + "\n".join(rows[:10]) + "\n[/TABLE]\n"
                return f"--- {label} ---\n{text}{table_text}"

            pages_text = []

            # 前 head_pages 页
            head_idx = list(range(min(head_pages, total)))
            for i in head_idx:
                pages_text.append(_extract_page(pdf.pages[i], f"Page {i+1} of {total}"))

            # 最后 tail_pages 页（不重复已抓的）
            tail_idx = list(range(max(head_pages, total - tail_pages), total))
            if tail_idx:
                pages_text.append(f"--- [Last {len(tail_idx)} page(s) — likely contains Summary/Balance] ---")
                for i in tail_idx:
                    pages_text.append(_extract_page(pdf.pages[i], f"Page {i+1} of {total} (LAST)"))

            return "\n\n".join(pages_text)
    except Exception as e:
        return f"[PDF preview error: {e}]"


def plan(pdf_path: str, skills_catalog_text: str) -> dict:
    """
    调用 Planner Agent (mimo-v2.5-pro, reasoning=high) 生成执行计划。
    返回 dict 包含 steps、extraction_approach、payee_buyer_strategy 等。
    """
    pdf_preview = _extract_pdf_preview(pdf_path)
    pdf_filename = Path(pdf_path).name

    system_prompt = """You are a Planner Agent in a Harness Engineering system.
Your job: analyze a bank statement PDF and produce a high-level plan for the Generator.

IMPORTANT: Focus on WHAT to achieve and key data insights — NOT HOW to implement it.
Do NOT specify implementation steps, library choices, or execution order.
The Generator will determine how to implement. Over-specifying causes cascading errors.

## Ground Truth Principle (Anthropic 2026)
Bank statements always contain a Summary or Balance section — this is the AUTHORITATIVE source of truth.
The Generator MUST use this summary to verify its extracted totals. If extracted totals don't match the
summary, the extraction is incomplete. Identify this section's location and format for the Generator.

Output ONLY valid JSON — no markdown fences, no explanation outside the JSON.

Output this exact JSON structure:
{
  "pdf_structure": "<concise description of the PDF layout — columns, format, bank name if visible>",
  "summary_section": {
    "location": "<where in the PDF is the summary/balance section — e.g. 'last page', 'top of page 1', 'not found'>",
    "format": "<what does the summary contain — e.g. 'Opening Balance, Total Debits, Total Credits, Closing Balance'>",
    "ground_truth_values": "<any specific totals visible in the preview — e.g. 'Total Debit: 12,345.00, Total Credit: 8,900.00'>",
    "verification_role": "Generator must extract these totals first, then verify all transaction rows sum to match"
  },
  "payee_buyer_strategy": "<how to identify Payee/Buyer from this specific PDF>",
  "column_mapping": {
    "date": "<exact column header for date>",
    "description": "<exact column header for description/narrative>",
    "debit": "<exact column header for debit/withdrawal>",
    "credit": "<exact column header for credit/deposit>",
    "payee": "<exact column header if exists, or 'derive from description'>"
  },
  "output_spec": {
    "sheets": [
      {
        "name": "By Payee & Buyer",
        "description": "All transactions grouped by Payee/Buyer — each group lists its transactions then a subtotal row"
      }
    ]
  },
  "objectives": [
    "Locate and extract the Summary/Balance section as ground truth for total Debit and Credit amounts",
    "Extract all individual transactions from the PDF accurately",
    "Identify the Payee/Buyer for each transaction",
    "Generate a single Excel sheet 'By Payee & Buyer' — transactions grouped by Payee/Buyer with subtotals",
    "Verify: sum of all extracted transactions must match the Summary totals"
  ]
}"""

    user_message = f"""Analyze this bank statement PDF and produce an execution plan.

**PDF filename**: {pdf_filename}
**PDF path**: {pdf_path}

**PDF Preview (first 3 pages)**:
{pdf_preview}

**Available Skills**:
{skills_catalog_text}

Produce the execution plan as JSON."""

    response = llm_client.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        model=llm_client.PLANNER_MODEL,
        reasoning="high",
        max_tokens=5000,
        stream=True,
        stream_label="[Planner]",
    )

    raw = llm_client.extract_text(response).strip()

    # 去掉可能的 markdown 代码块
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 降级：返回通用计划
        print(f"  [Planner] Warning: could not parse JSON, using default plan. Raw: {raw[:200]}")
        return {
            "pdf_structure": "Unknown — using default extraction",
            "extraction_approach": "pdfplumber tables then text fallback",
            "payee_buyer_strategy": "Extract from Description column or dedicated Payee column",
            "column_mapping": {
                "date": "Date",
                "description": "Description",
                "debit": "Debit",
                "credit": "Credit",
                "payee": "derive from description",
            },
            "output_spec": {
                "sheets": [{"name": "By Payee & Buyer", "description": "Grouped by payee with subtotals"}]
            },
            "objectives": [
                "Extract all transactions from the PDF accurately",
                "Identify the Payee/Buyer for each transaction",
                "Generate Excel with one sheet 'By Payee & Buyer' — grouped transactions with subtotals",
            ],
            "_raw_response": raw,
        }


if __name__ == "__main__":
    import sys
    import skill_loader

    if len(sys.argv) < 2:
        print("Usage: python planner.py <pdf_path>")
        sys.exit(1)

    catalog = skill_loader.load()
    skills_text = skill_loader.format_for_prompt(catalog)
    result = plan(sys.argv[1], skills_text)
    print(json.dumps(result, indent=2, ensure_ascii=False))
