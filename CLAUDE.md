# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**BankstatementAI** is an agentic system that extracts transaction data from bank statement PDFs and generates structured Excel output. It uses a Generator → Evaluator feedback loop (max 3 iterations) with extended reasoning to iteratively refine results until a quality threshold is reached.

## Quick Start

### Run the main pipeline
```bash
python3 main.py <bank_statement.pdf> [output.xlsx]
```

Example:
```bash
python3 main.py "Bank Statement Report 2025-10-31.pdf" output.xlsx
```

The system will:
1. Extract transactions from the PDF using the Generator agent (with reasoning=high)
2. Evaluate output quality using the Evaluator agent
3. If score < 12.0/12, return feedback to Generator for iteration 2
4. Repeat up to MAX_ITERATIONS (3 by default) until quality passes or max iterations reached

### Configuration

Edit `.env` to configure:
- `MIMO_API_KEY` — MiMo API credentials
- `MIMO_BASE_URL` — API endpoint (default: token-plan-sgp.xiaomimimo.com/v1)
- `MIMO_PLANNER_MODEL`, `MIMO_EXECUTOR_MODEL`, `MIMO_GENERATOR_MODEL`, `MIMO_EVALUATOR_MODEL` — model assignments

## Architecture

### Core Agents (Agentic Loop)

**main.py (Orchestrator)**
- Controls the 3-iteration Generator → Evaluator → Feedback loop
- Displays formatted output with ANSI colors, token usage summary, quality scoring
- Entry point: `run(pdf_path, output_path) → dict`

**generator.py (Single Unified Agent)**
- Analyzes PDF structure with `reasoning=high` (extended thinking)
- Uses Agent Computer Interface (ACI) tools: `list_directory`, `read_file`, `search_file`, `run_python`
- Progressively discovers available Skills from `Skills/` directory (no pre-loading)
- Generates Excel output with transaction data
- Maintains scratchpad (`scratch.md`) for persistent memory across context clears
- Returns: `{success, summary, tool_calls, error}`

**evaluator.py (Quality Assessment)**
- Verifies 5 criteria: file existence, Excel structure, transaction count, amount correctness, payee/buyer capture
- Uses `reasoning=medium` for verification logic
- Emits score (0–12), pass/fail, strengths, issues, and feedback for next iteration
- Returns: `{score, passed, criteria, strengths, issues, feedback}`

**planner.py (Deprecated)**
- Kept for reference only; functionality moved to generator.py with `reasoning=high`
- Do not call or import

### Supporting Modules

**llm_client.py**
- OpenAI-compatible wrapper for MiMo API
- Implements prompt caching: system prompt + tools cached at context level → 99.2% cheaper reads
- Functions: `get_client()`, `_apply_cache_control()`, token/reasoning logging
- Reasoning budgets: `high` (30k tokens), `medium` (15k), `low` (3k), `none` (disabled)

**skill_loader.py**
- Loads custom Skills from `Skills/` directory (JSON + Markdown specs)
- Provides progressive disclosure: Skills are read on-demand by Generator, not pre-loaded
- Returns formatted catalog text for agent discovery

**tools.py (ACI Implementations)**
- `list_directory(path, pattern?)` — list files/dirs (much faster than Python subprocess)
- `read_file(path, start_line?, end_line?, max_chars?)` — read with optional range limits
- `search_file(path, query)` — grep-like search across files
- `run_python(code)` — sandboxed execution, 60-second timeout
- Respects Anthropic ACI design principles: tool descriptions include examples, edge cases, and limits

**main.py Utilities**
- `_cleanup_workspace()` — removes temporary files (*.txt, *.json, *.log, *.md, *.csv) and `__pycache__` on startup
- Keeps core files: main.py, generator.py, executor.py, evaluator.py, planner.py, tools.py, llm_client.py, skill_loader.py, .env
- Pretty printing: ANSI colors, progress bars, score visualization

## Key Design Patterns

### Generator-Evaluator Loop (Anthropic Plan A)
```
Iteration 1:  Generator → creates Excel → Evaluator → score < 12 ?
Iteration 2:  Generator + Evaluator Feedback → refine → Evaluator → score < 12 ?
Iteration 3:  Generator + Feedback → final → Evaluator → score or max_iterations
```

**Feedback Strategy**: When score < PASS_THRESHOLD (12.0), Evaluator returns structured feedback (e.g., "Transaction count mismatch: expected X, got Y"). Generator gets fresh context (no conversation history) + feedback + prev_summary (previous iteration's summary stats).

### Extended Reasoning (reasoning=high)
- Generator uses `reasoning=high` (30k token budget) to internally reason about PDF structure before calling tools
- No explicit Planner phase; reasoning replaces it
- Evaluator uses `reasoning=medium` (15k tokens) for verification

### Prompt Caching (99.2% Savings)
- System prompt + tool definitions cached across agentic loop rounds
- First round: cache write (free); rounds 2–N: cache read at 1/120 standard input cost
- Implemented in `llm_client._apply_cache_control()`: adds `cache_control: {type: ephemeral}` to system and final tool

### Scratchpad Pattern (scratch.md)
- Generator stores intermediate findings, transaction summaries, and parsed data in `scratch.md`
- Persists across context clears (when token count exceeds CONTEXT_CLEAR_THRESHOLD)
- Read at loop start to recover state without re-parsing the entire PDF

### Progressive Disclosure (Skills)
- Skills are NOT pre-loaded; Generator discovers and reads them on-demand
- Reduces context bloat and lets Generator prioritize only relevant skills
- `skill_loader.load()` returns catalog; Generator calls `read_file("Skills/...")` as needed

## Workspace Cleanup

On every run, `_cleanup_workspace()` removes:
- Temporary files: generate_*.txt, extract_*.json, output_*.xlsx, tmp_*.md, scratch_*.md, temp_*.py, logs_*.log
- File extensions: .txt, .json, .log, .md, .csv (except in keep list)
- __pycache__/ directory

This ensures a clean slate for each execution without manual file management.

## Error Handling & Iteration Control

- **MAX_ITERATIONS** = 3 (configurable in main.py)
- **PASS_THRESHOLD** = 12.0 (full score on 12-point scale)
- If Generator fails (no Excel created), feedback is: "The Excel file was not created at {path}. Make sure to save the file."
- If Evaluator fails, pipeline returns error with iteration count

## Token & Cost Tracking

- `llm_client.reset_token_log()` and `llm_client.get_token_log()` track all API calls
- `llm_client.reset_reasoning_log()` tracks extended reasoning spend
- Output includes per-phase (Generator, Evaluator) breakdown:
  - API calls, input tokens, completion tokens, cache_read_tokens, cache_creation_tokens
  - Total context utilization vs. 1M token MiMo limit
  - Cache savings estimate (if cache_read > 0)

## Debugging & Logs

- **Logs directory**: `logs/run_<YYYYMMDD_HHMMSS>/` created per run
- Each phase (Generator iteration 1–3, Evaluator) may write log files (inspect with `run_python` or Read tool)
- Scratchpad: `scratch.md` for Generator's persistent notes (survives context clears)

## Workspace Files to Preserve

**Core system files** (never delete):
- main.py, generator.py, evaluator.py, executor.py, planner.py
- tools.py, llm_client.py, skill_loader.py
- .env (contains API credentials)
- CLAUDE.md, Skills/ directory, logs/ directory

**Sample inputs** (optional, for testing):
- Bank Statement Report 2025-10-31.pdf, Juru Jaya Trading GL 2025.xlsx

## Design Principles

1. **Anthropic ACI**: Tools designed for agents, not developers. Descriptions include examples, edge cases, and limits.
2. **Extended Reasoning**: Generator uses reasoning=high to plan before tool execution; no separate planner step.
3. **Feedback-Driven Refinement**: Evaluator provides concrete, actionable feedback; Generator gets fresh context each iteration.
4. **Cost Optimization**: Prompt caching + progressive skills loading minimize token spend.
5. **Deterministic Cleanup**: Temp files removed on startup; workspace is fresh for each run.
6. **Observable Quality**: Score bar, criteria breakdown, strengths/issues displayed; token usage logged per phase.
