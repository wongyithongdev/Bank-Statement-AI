# Bank Statement AI

An agentic system that extracts transaction data from bank statement PDFs and generates structured Excel output using LLM extended reasoning and iterative quality refinement.

## What It Does

Given a bank statement PDF, this tool:
1. Parses all transactions (date, description, debit, credit, balance)
2. Identifies payees and buyers from transaction descriptions
3. Outputs a structured Excel file with clean, categorized data
4. Self-evaluates quality and retries up to 3 times if score < 12/12

## Quick Start

### Prerequisites

```bash
pip install openai python-dotenv pdfplumber openpyxl pandas
```

### Setup

Create a `.env` file:
```env
MIMO_API_KEY=your_api_key_here
MIMO_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1
MIMO_GENERATOR_MODEL=mimo-v2.5-pro
MIMO_EVALUATOR_MODEL=mimo-v2.5-pro
```

### Run

```bash
python3 main.py <bank_statement.pdf> [output.xlsx]
```

**Example:**
```bash
python3 main.py "Bank Statement Report 2025-10-31.pdf" output.xlsx
```

## Architecture

```
main.py (Orchestrator)
  │
  ├── [1] skill_loader.py     — discover available Skills/
  │
  ├── [2] generator.py        — parse PDF → create Excel
  │   ├── reasoning=high      — internal extended thinking
  │   ├── tools: list_dir, read_file, run_python
  │   └── scratch.md          — persistent memory across context clears
  │
  ├── [3] evaluator.py        — verify quality (0–12 score)
  │   └── 5 criteria: file, structure, count, amounts, payees
  │
  └── [4] feedback loop       — if score < 12, retry up to 3×
```

## Quality Scoring (12 points total)

| Criterion | Points | Description |
|-----------|--------|-------------|
| File existence | 2 | Output Excel file created |
| Excel structure | 2 | Correct columns and sheet format |
| Transaction count | 2 | Matches PDF transaction count |
| Amount accuracy | 4 | Debit/credit totals match PDF summary |
| Payee capture | 2 | Payee/buyer names correctly identified |

## Project Structure

```
.
├── main.py          # Orchestrator & CLI entry point
├── generator.py     # PDF analysis + Excel generation agent
├── evaluator.py     # Quality assessment agent
├── llm_client.py    # MiMo API wrapper with prompt caching
├── tools.py         # ACI tools (list_dir, read_file, run_python)
├── skill_loader.py  # Progressive skills discovery
├── Skills/          # Domain-specific skill libraries
│   └── xlsx/        # Excel + PDF processing skills
└── .env             # API credentials (not committed)
```

## Cost Optimization

- **Prompt caching**: System prompt + tool definitions cached → 99.2% cheaper on cache hits
- **Progressive disclosure**: Skills loaded on-demand, not pre-loaded
- **Extended reasoning**: Generator uses internal planning (reasoning=high) instead of a separate planner agent

## Notes

- `.env` is not committed — never share your API key
- Temporary files (`scratch.md`, `generate_*.txt`, etc.) are auto-cleaned on each run
- Logs are saved to `logs/run_<timestamp>/` for debugging
