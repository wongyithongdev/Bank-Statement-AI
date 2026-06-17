"""
Worker entrypoint — runs inside an isolated Docker container per task.

Environment variables required:
  TASK_ID, POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER,
  POSTGRES_PASSWORD, MIMO_API_KEY, MIMO_BASE_URL, DATA_DIR,
  OBJECT_SERVER_API_KEY
"""
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import httpx

# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_conn():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", 5432)),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event(conn, task_id: str, event: dict) -> None:
    event.setdefault("timestamp", _now())
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE bankstatement
            SET stream_events = stream_events || %s::jsonb,
                updated_at    = now()
            WHERE task_id = %s
            """,
            (json.dumps([event]), task_id),
        )
    conn.commit()


def _update_status(conn, task_id: str, status: str, **kwargs) -> None:
    fields = {"status": status, "updated_at": "now()"}
    sets = ["status = %s", "updated_at = now()"]
    values = [status]

    for col in ("score", "iterations", "xlsx_path", "file_link", "error"):
        if col in kwargs and kwargs[col] is not None:
            sets.append(f"{col} = %s")
            values.append(kwargs[col])

    values.append(task_id)
    sql = f"UPDATE bankstatement SET {', '.join(sets)} WHERE task_id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, values)
    conn.commit()


def _fetch_task(conn, task_id: str) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT task_id, book_id, user_id, pdf_path, task_name FROM bankstatement WHERE task_id = %s",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"Task {task_id} not found in database")
    return dict(row)


def _upload_xlsx_to_object_server(xlsx_path: str, task_name: str, book_id: str) -> str:
    """Upload Excel file to Object Server, return the file link."""
    api_key = os.environ.get("OBJECT_SERVER_API_KEY")
    if not api_key:
        raise RuntimeError("OBJECT_SERVER_API_KEY not set")

    with open(xlsx_path, "rb") as f:
        file_bytes = f.read()

    response = httpx.post(
        "https://files.my365biz.com/upload",
        files={"file": (f"{task_name}.xlsx", file_bytes), "bookid": (None, book_id)},
        headers={"X-API-Key": api_key},
        timeout=60.0,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Object Server upload failed: {response.status_code} {response.text}"
        )

    data = response.json()
    return data["link"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    task_id = os.environ.get("TASK_ID")
    if not task_id:
        print("ERROR: TASK_ID environment variable not set", file=sys.stderr)
        sys.exit(1)

    data_dir = os.environ.get("DATA_DIR", "/data/bankstatement")

    conn = _get_conn()
    task = _fetch_task(conn, task_id)

    pdf_path = task["pdf_path"]
    output_dir = Path(data_dir) / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = str(output_dir / f"{task_id}.xlsx")

    # Setup isolated sandbox for this task
    from worker.sandbox import setup as setup_sandbox, teardown as teardown_sandbox
    sandbox_dir = setup_sandbox(task_id)
    log_dir = str(Path(sandbox_dir) / "logs")

    # Point tools to sandbox so scratch.md and relative paths are isolated
    from internal import tools as tool_module
    tool_module.set_project_root(sandbox_dir)

    # Mark as processing
    _update_status(conn, task_id, "processing")
    _append_event(conn, task_id, {"event": "task_started"})

    try:
        from internal import skill_loader, generator, evaluator, llm_client

        llm_client.reset_token_log()
        llm_client.reset_reasoning_log()

        catalog = skill_loader.load(skills_root=str(Path("/app/Skills")))
        skills_text = skill_loader.format_for_discovery(catalog)

        MAX_ITERATIONS = 3
        PASS_THRESHOLD = 12.0

        feedback = None
        prev_summary = None
        final_score = 0.0
        final_passed = False

        for iteration in range(1, MAX_ITERATIONS + 1):
            _append_event(conn, task_id, {"event": "iteration_start", "iteration": iteration})

            gen_result = generator.generate(
                pdf_path=pdf_path,
                output_path=xlsx_path,
                skills_catalog_text=skills_text,
                feedback=feedback,
                prev_summary=prev_summary,
                log_dir=log_dir,
                iteration=iteration,
                sandbox_dir=sandbox_dir,
            )

            if not gen_result.get("success"):
                feedback = (
                    f"The Excel file was not created at {xlsx_path}. "
                    "Make sure to save the file before finishing."
                )
                _append_event(conn, task_id, {
                    "event": "iteration_complete",
                    "iteration": iteration,
                    "score": 0,
                    "passed": False,
                    "issues": [gen_result.get("error", "Generator failed")],
                    "strengths": [],
                    "criteria": {},
                })
                _update_status(conn, task_id, "processing", iterations=iteration)
                continue

            eval_result = evaluator.evaluate(
                xlsx_path,
                pdf_path,
                gen_summary=gen_result.get("summary"),
                log_dir=log_dir,
                iteration=iteration,
            )

            score = eval_result["score"]
            passed = eval_result["passed"]
            final_score = score
            final_passed = passed

            _append_event(conn, task_id, {
                "event": "iteration_complete",
                "iteration": iteration,
                "score": score,
                "passed": passed,
                "criteria": eval_result.get("criteria", {}),
                "issues": eval_result.get("issues", []),
                "strengths": eval_result.get("strengths", []),
            })
            _update_status(conn, task_id, "processing", score=score, iterations=iteration)

            if passed:
                break
            elif iteration < MAX_ITERATIONS:
                feedback = eval_result.get("feedback", "Please improve the output quality.")
                prev_summary = gen_result.get("summary", {})

        # Upload Excel to Object Server if task passed
        file_link = None
        if final_passed and Path(xlsx_path).exists():
            try:
                file_link = _upload_xlsx_to_object_server(
                    xlsx_path,
                    task["task_name"] or task_id,
                    task["book_id"],
                )
                print(f"Excel uploaded to Object Server: {file_link}")
            except RuntimeError as exc:
                print(f"Warning: Excel upload failed: {exc}", file=sys.stderr)
                file_link = None

        # Final status
        _update_status(
            conn, task_id,
            status="completed",
            score=final_score,
            iterations=iteration,
            xlsx_path=xlsx_path,
            file_link=file_link,
        )
        _append_event(conn, task_id, {
            "event": "task_complete",
            "score": final_score,
            "passed": final_passed,
            "iterations": iteration,
            "file_link": file_link,
        })

    except Exception as exc:
        err = traceback.format_exc()
        print(err, file=sys.stderr)
        _update_status(conn, task_id, "failed", error=str(exc))
        _append_event(conn, task_id, {"event": "task_failed", "error": str(exc)})
    finally:
        conn.close()
        teardown_sandbox(task_id)


if __name__ == "__main__":
    main()
