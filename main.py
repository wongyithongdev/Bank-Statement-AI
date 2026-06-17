"""
Harness 控制器 (main.py)
用法: python3 main.py <bank_statement.pdf> [output.xlsx]
"""

import sys
import json
import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from internal import skill_loader
from internal import generator
from internal import evaluator
from internal import llm_client

MAX_ITERATIONS = 3
PASS_THRESHOLD = 12.0

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BLUE   = "\033[34m"
MAGENTA= "\033[35m"
WHITE  = "\033[97m"

W = 70  # terminal width


def _hr(char="─", color=DIM):
    print(f"{color}{char * W}{RESET}")

def _section(icon, title, color=CYAN):
    print()
    _hr("─", DIM)
    print(f"{color}{BOLD} {icon}  {title}{RESET}")
    _hr("─", DIM)

def _kv(key, value, indent=4, key_color=DIM, val_color=WHITE):
    pad = " " * indent
    print(f"{pad}{key_color}{key:<22}{RESET}{val_color}{value}{RESET}")

def _bullet(text, indent=4, color=WHITE):
    print(f"{' ' * indent}{DIM}•{RESET} {color}{text}{RESET}")

def _ok(text):
    print(f"  {GREEN}{BOLD}✓{RESET}  {WHITE}{text}{RESET}")

def _warn(text):
    print(f"  {YELLOW}{BOLD}⚠{RESET}  {YELLOW}{text}{RESET}")

def _err(text):
    print(f"  {RED}{BOLD}✗{RESET}  {RED}{text}{RESET}")

def _score_bar(score, total=12, width=20):
    filled = int(round(score / total * width))
    bar = "█" * filled + "░" * (width - filled)
    color = GREEN if score >= 8 else YELLOW if score >= 5 else RED
    return f"{color}{bar}{RESET} {BOLD}{score:.1f}/{total}{RESET}"


def _cleanup_workspace():
    """每次 CLI 启动时清理上一次遗留的临时文件，确保全新起点。"""
    root = Path(__file__).parent
    # 需要保留的核心文件（不删）
    keep = {
        "main.py", "generator.py", "executor.py", "evaluator.py", "planner.py",
        "tools.py", "llm_client.py", "skill_loader.py",
        ".env",
    }
    # 删除扩展名匹配 + 不在保留名单的文件
    temp_extensions = {".txt", ".json", ".log", ".md", ".csv"}
    temp_prefixes   = ("generate_", "extract_", "output_", "tmp_", "temp_",
                       "scratch", "transactions", "logs")

    removed = []
    for f in root.iterdir():
        if not f.is_file():
            continue
        if f.name in keep:
            continue
        if f.name.startswith("."):
            continue
        if f.suffix in temp_extensions or any(f.name.startswith(p) for p in temp_prefixes):
            f.unlink()
            removed.append(f.name)

    # 清理 __pycache__
    pycache = root / "__pycache__"
    if pycache.exists():
        import shutil
        shutil.rmtree(pycache)
        removed.append("__pycache__/")

    if removed:
        print(f"  {DIM}[Sandbox] Cleaned {len(removed)} leftover file(s): "
              f"{', '.join(removed[:5])}{'…' if len(removed) > 5 else ''}{RESET}")


def run(pdf_path: str, output_path: str = None) -> dict:
    _cleanup_workspace()

    pdf_path = str(Path(pdf_path).resolve())
    if not Path(pdf_path).exists():
        _err(f"PDF not found: {pdf_path}")
        return {"success": False, "error": f"PDF not found: {pdf_path}"}

    if output_path is None:
        stem = Path(pdf_path).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(Path(__file__).parent / f"output_{stem}_{timestamp}.xlsx")
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    log_dir = str(Path(__file__).parent / "logs" / f"run_{timestamp}")
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    llm_client.reset_token_log()
    llm_client.reset_reasoning_log()

    # ── Header banner ─────────────────────────────────────────────────────────
    print()
    _hr("═", CYAN)
    print(f"{CYAN}{BOLD}{'  Bank Statement PDF → Payee & Buyer Excel':^{W}}{RESET}")
    _hr("═", CYAN)
    _kv("Input",     Path(pdf_path).name,   key_color=DIM, val_color=WHITE)
    _kv("Output",    output_path,            key_color=DIM, val_color=WHITE)
    _kv("Logs",      log_dir,               key_color=DIM, val_color=DIM)
    _kv("Generator", f"{llm_client.GENERATOR_MODEL}  (reasoning=medium)", key_color=DIM, val_color=BLUE)
    _kv("Evaluator", f"{llm_client.EVALUATOR_MODEL}  (reasoning=medium)", key_color=DIM, val_color=MAGENTA)
    _hr("═", CYAN)

    # ── [1] Skill Discovery (Progressive Disclosure Layer 1) ─────────────────
    catalog = skill_loader.load()
    skills_text = skill_loader.format_for_discovery(catalog)

    # ── [2-4] Generator + Evaluator loop ─────────────────────────────────────
    feedback = None
    prev_summary = None
    final_result = None

    for iteration in range(1, MAX_ITERATIONS + 1):
        _section(f"①", f"Generator  —  iteration {iteration}/{MAX_ITERATIONS}", BLUE)
        if feedback:
            print(f"  {YELLOW}Applying Evaluator feedback (fresh context — Anthropic Plan A):{RESET}")
            for line in feedback.replace(". ", ".\n").split("\n"):
                if line.strip():
                    _bullet(line.strip(), color=YELLOW)
        print()

        gen_result = generator.generate(
            pdf_path=pdf_path,
            output_path=output_path,
            skills_catalog_text=skills_text,
            feedback=feedback,
            prev_summary=prev_summary,
            log_dir=log_dir,
            iteration=iteration,
        )

        print()
        _kv("Tool calls used", str(gen_result["tool_calls"]), key_color=DIM, val_color=WHITE)
        _kv("Output created",  str(gen_result["success"]),    key_color=DIM, val_color=GREEN if gen_result["success"] else RED)

        if gen_result.get("summary") and gen_result["summary"].get("summary_debit") is not None:
            s = gen_result["summary"]
            _kv("Summary Debit",    f"RM {s.get('summary_debit', 'N/A')}", key_color=DIM, val_color=WHITE)
            _kv("Summary Credit",   f"RM {s.get('summary_credit', 'N/A')}", key_color=DIM, val_color=WHITE)
            _kv("Extracted Debit",  f"RM {s.get('extracted_debit', 'N/A')}", key_color=DIM, val_color=WHITE)
            _kv("Extracted Credit", f"RM {s.get('extracted_credit', 'N/A')}", key_color=DIM, val_color=WHITE)

        if not gen_result["success"]:
            _err(f"Generator failed: {gen_result.get('error')}")
            if iteration == MAX_ITERATIONS:
                return {"success": False, "error": gen_result.get("error"), "iterations": iteration}
            feedback = f"The Excel file was not created at {output_path}. Make sure to save the file."
            continue

        # ── [2] Evaluator ─────────────────────────────────────────────────
        _section("②", "Evaluator  —  independent quality assessment", MAGENTA)
        eval_result = evaluator.evaluate(
            output_path,
            pdf_path,
            gen_summary=gen_result.get("summary"),
            log_dir=log_dir,
            iteration=iteration,
        )
        score  = eval_result["score"]
        passed = eval_result["passed"]

        print()
        print(f"    Score  {_score_bar(score)}")
        print()

        if eval_result.get("criteria"):
            print(f"    {DIM}Criteria breakdown:{RESET}")
            for criterion, val in eval_result["criteria"].items():
                bar_filled = "█" * val + "░" * (2 - val)
                color = GREEN if val == 2 else YELLOW if val == 1 else RED
                print(f"      {DIM}{criterion:<25}{RESET}{color}{bar_filled}{RESET}  {BOLD}{val}/2{RESET}")

        if eval_result.get("strengths"):
            print(f"\n    {GREEN}Strengths:{RESET}")
            for s in eval_result["strengths"]:
                _bullet(s[:90], color=GREEN)

        if eval_result.get("issues"):
            print(f"\n    {YELLOW}Issues:{RESET}")
            for issue in eval_result["issues"]:
                _bullet(issue[:90], color=YELLOW)

        final_result = {
            "success": True,
            "output_path": output_path,
            "score": score,
            "passed": passed,
            "iterations": iteration,
            "eval": eval_result,
        }

        # ── [3] Feedback decision ─────────────────────────────────────────
        _section("③", "Feedback Loop", CYAN)
        if passed:
            _ok(f"Quality passed  ({score:.1f}/10)  —  output is ready!")
            break
        elif iteration < MAX_ITERATIONS:
            _warn(f"Score {score:.1f} < {PASS_THRESHOLD}  —  sending feedback to Generator (iteration {iteration+1})")
            feedback = eval_result.get("feedback", "Please improve the output quality.")
            prev_summary = gen_result.get("summary", {})  # artifact 传给下一轮
            if feedback:
                print(f"\n    {DIM}Feedback (full):{RESET}")
                for _sent in feedback.replace(". ", ".\n").split("\n"):
                    if _sent.strip():
                        _bullet(_sent.strip(), color=YELLOW)
        else:
            _warn(f"Max iterations reached.  Final score: {score:.1f}/10")

    # ── Token Usage Summary ───────────────────────────────────────────────────
    token_log = llm_client.get_token_log()
    if token_log:
        _section("📊", "Token Usage Summary", CYAN)
        total_prompt = total_completion = total_all = 0
        total_cache_read = total_cache_write = 0
        phase_totals: dict[str, dict] = {}

        for entry in token_log:
            lbl = entry["label"]
            if "[Generator" in lbl:
                phase = "Generator"
            elif "[Evaluator]" in lbl:
                phase = "Evaluator"
            else:
                phase = lbl

            if phase not in phase_totals:
                phase_totals[phase] = {"prompt": 0, "completion": 0, "calls": 0,
                                       "cache_read": 0, "cache_write": 0}
            phase_totals[phase]["prompt"]      += entry["prompt_tokens"]
            phase_totals[phase]["completion"]  += entry["completion_tokens"]
            phase_totals[phase]["calls"]       += 1
            phase_totals[phase]["cache_read"]  += entry.get("cache_read_tokens", 0)
            phase_totals[phase]["cache_write"] += entry.get("cache_creation_tokens", 0)
            total_prompt      += entry["prompt_tokens"]
            total_completion  += entry["completion_tokens"]
            total_all         += entry["total_tokens"]
            total_cache_read  += entry.get("cache_read_tokens", 0)
            total_cache_write += entry.get("cache_creation_tokens", 0)

        phase_colors = {"Generator": BLUE, "Evaluator": MAGENTA}
        has_cache = total_cache_read > 0 or total_cache_write > 0

        print()
        if has_cache:
            print(f"    {DIM}{'Phase':<16}{'Calls':>7}{'Input':>11}{'Output':>10}{'CacheRead':>11}{'CacheWrt':>10}{'Total':>11}{RESET}")
            print(f"    {DIM}{'─'*16}{'─'*7}{'─'*11}{'─'*10}{'─'*11}{'─'*10}{'─'*11}{RESET}")
        else:
            print(f"    {DIM}{'Phase':<16}{'API Calls':>10}{'Input':>12}{'Output':>12}{'Total':>12}{RESET}")
            print(f"    {DIM}{'─'*16}{'─'*10}{'─'*12}{'─'*12}{'─'*12}{RESET}")

        for phase, data in phase_totals.items():
            color = phase_colors.get(phase, WHITE)
            t = data["prompt"] + data["completion"]
            if has_cache:
                cr = data["cache_read"]
                cw = data["cache_write"]
                cr_str = f"{GREEN}{cr:>10,}{RESET}" if cr > 0 else f"{DIM}{'0':>10}{RESET}"
                cw_str = f"{DIM}{cw:>9,}{RESET}"
                print(f"    {color}{BOLD}{phase:<16}{RESET}"
                      f"{DIM}{data['calls']:>7}{RESET}"
                      f"  {WHITE}{data['prompt']:>9,}{RESET}"
                      f"  {WHITE}{data['completion']:>8,}{RESET}"
                      f"  {cr_str}"
                      f"  {cw_str}"
                      f"  {BOLD}{t:>9,}{RESET}")
            else:
                print(f"    {color}{BOLD}{phase:<16}{RESET}"
                      f"{DIM}{data['calls']:>10}{RESET}"
                      f"  {WHITE}{data['prompt']:>10,}{RESET}"
                      f"  {WHITE}{data['completion']:>10,}{RESET}"
                      f"  {BOLD}{t:>10,}{RESET}")

        if has_cache:
            print(f"    {DIM}{'─'*16}{'─'*7}{'─'*11}{'─'*10}{'─'*11}{'─'*10}{'─'*11}{RESET}")
            print(f"    {BOLD}{'TOTAL':<16}{RESET}"
                  f"{'':>7}"
                  f"  {WHITE}{total_prompt:>9,}{RESET}"
                  f"  {WHITE}{total_completion:>8,}{RESET}"
                  f"  {GREEN}{BOLD}{total_cache_read:>9,}{RESET}"
                  f"  {DIM}{total_cache_write:>9,}{RESET}"
                  f"  {CYAN}{BOLD}{total_all:>9,}{RESET}")
            # 缓存节省估算（cache read 比标准 input 便宜 99.2%）
            saved_tokens = total_cache_read
            if saved_tokens > 0:
                print(f"\n    {GREEN}{BOLD}Cache savings:{RESET}  "
                      f"{GREEN}{saved_tokens:,} tokens served from cache  "
                      f"(~99% cheaper than standard input){RESET}")
        else:
            print(f"    {DIM}{'─'*16}{'─'*10}{'─'*12}{'─'*12}{'─'*12}{RESET}")
            print(f"    {BOLD}{'TOTAL':<16}{RESET}"
                  f"{'':>10}"
                  f"  {WHITE}{total_prompt:>10,}{RESET}"
                  f"  {WHITE}{total_completion:>10,}{RESET}"
                  f"  {CYAN}{BOLD}{total_all:>10,}{RESET}")

        # 占 1M context 的百分比（估算）
        pct = total_all / 1_048_576 * 100
        print(f"\n    {DIM}MiMo 1M context utilisation:{RESET}  "
              f"{CYAN}{BOLD}{pct:.2f}%{RESET}  {DIM}({total_all:,} / 1,048,576 tokens){RESET}")

    # ── Final summary ─────────────────────────────────────────────────────────
    print()
    _hr("═", CYAN)
    if final_result and final_result["success"]:
        _ok(f"Done!  {output_path}")
        print(f"    {DIM}Score :{RESET}  {_score_bar(final_result['score'])}")
        print(f"    {DIM}Loops :{RESET}  {WHITE}{final_result['iterations']}/{MAX_ITERATIONS}{RESET}")
    else:
        _err("Pipeline did not produce a valid output.")
    _hr("═", CYAN)
    print()

    return final_result or {"success": False, "error": "No result produced"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"\n  {BOLD}Usage:{RESET}  python3 main.py <bank_statement.pdf> [output.xlsx]\n")
        print(f"  {DIM}Example:{RESET}  python3 main.py maybank_statement.pdf output.xlsx\n")
        sys.exit(1)

    if not os.environ.get("MIMO_API_KEY"):
        _err("MIMO_API_KEY not set. Check your .env file.")
        sys.exit(1)

    pdf_arg = sys.argv[1]
    out_arg = sys.argv[2] if len(sys.argv) > 2 else None

    result = run(pdf_arg, out_arg)
    sys.exit(0 if result.get("success") else 1)
