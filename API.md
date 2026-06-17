# BankstatementAI API Documentation

## Overview

**BankstatementAI** is a production-grade HTTP API that extracts transaction data from bank statement PDFs and generates structured Excel output using agentic AI with per-task Docker isolation.

- **Base URL**: `http://localhost:8001/api/v1/bankstatement`
- **Authentication**: Bearer token (Keycloak/AuthServer)
- **Rate Limit**: 85 tasks/minute (MiMo API RPM limit)
- **Queue**: Redis-backed, survives API restarts

---

## Authentication

All endpoints require an `Authorization: Bearer <access_token>` header.

### Token Verification Flow

```
User Request (Authorization: Bearer $TOKEN)
    ↓
API calls AuthServer: GET /api/v1/users/me
    ↓
AuthServer returns: {user_id, books[], email, ...}
    ↓
Extract user's current book: GET /auth/api/v1/users/me/current-book
    ↓
Verify permission: POST /api/v1/permissions/check {book_id, action}
    ↓
Request proceeds or 401/403 returned
```

**Error Responses:**
- `401 Unauthorized`: Invalid or expired token
- `502 Bad Gateway`: AuthServer unreachable

---

## Core Workflow

```
1. Client uploads PDF (form-data)
   ↓
2. API enqueues task in Redis (LPUSH)
   ↓ Returns 202 ACCEPTED immediately
   
3. Background dispatcher dequeues task (respects RPM=85 limit)
   ↓
4. Worker container spawned with isolated sandbox
   ↓
5. Generator iterates (max 3 rounds): PDF → Excel
   ↓
6. Evaluator scores quality (0-12 scale)
   ↓
7. If score ≥ 12: passed, upload to Object Server
   ↓ Else: re-run with feedback or fail after 3 rounds
   ↓
8. Task status updated to "completed" or "failed"
   ↓
9. Client polls SSE stream or task detail to check progress
   ↓
10. Client downloads Excel from Object Server when ready
```

---

## Endpoints

### 1. Upload Bank Statement PDF

**POST** `/upload`

Upload a bank statement PDF for processing. The file is uploaded to Object Server, enqueued for dispatch, and a background worker processes it asynchronously.

#### Request

```http
POST /api/v1/bankstatement/upload HTTP/1.1
Authorization: Bearer {access_token}
Content-Type: multipart/form-data

file: @statement.pdf (required, PDF file)
task_name: "Jan 2026 Statement" (optional, string, max 255 chars)
```

#### Response (202 Accepted)

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "task_name": "Jan 2026 Statement",
  "created_at": "2026-06-17T18:30:00Z"
}
```

#### Errors

| Code | Message | Reason |
|------|---------|--------|
| 401 | Unauthorized | Missing or invalid token |
| 403 | No active book selected | User has no books or book context missing |
| 403 | Access denied | User lacks `book.write` permission on the book |
| 422 | Only PDF files are accepted | Uploaded file is not a PDF |
| 502 | AuthServer unreachable | Identity or permission check failed |
| 502 | Failed to upload file to Object Server | Object Server upload failed |

#### Notes

- Book ID is determined automatically from AuthServer's `/auth/api/v1/users/me/current-book` endpoint
- No `book_id` parameter needed in the request
- Task immediately enqueued in Redis; worker dispatch respects RPM=85 limit
- Returns immediately (202) — does not wait for processing

---

### 2. List Tasks for User's Books

**GET** `/tasks`

List all bank statement tasks for the authenticated user's books.

#### Request

```http
GET /api/v1/bankstatement/tasks HTTP/1.1
Authorization: Bearer {access_token}
```

#### Response (200 OK)

```json
[
  {
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "task_name": "Jan 2026 Statement",
    "book_id": "book_mnIDVn39cOk",
    "user_id": "keycloak-user-abc123def456",
    "status": "completed",
    "created_at": "2026-06-17T18:30:00Z"
  },
  {
    "task_id": "660e8400-e29b-41d4-a716-446655440001",
    "task_name": "Dec 2025 Statement",
    "book_id": "book_mnIDVn39cOk",
    "user_id": "keycloak-user-abc123def456",
    "status": "processing",
    "created_at": "2026-06-16T14:00:00Z"
  }
]
```

#### Field Descriptions

- `task_id` — Unique task UUID
- `task_name` — Human-readable label (optional, provided at upload time)
- `book_id` — Book this task belongs to (from AuthServer current-book)
- `user_id` — Keycloak user ID (UUID, not username)
- `status` — Current state of the task

#### Status Values

- `queued` — Task waiting in Redis queue for dispatch
- `processing` — Worker is running generator + evaluator loop
- `completed` — Task finished; quality threshold passed (score ≥ 12.0)
- `failed` — Task failed (quality threshold never reached or worker error)

#### Notes

- Filtered automatically to books the user has access to (from AuthServer)
- Ordered by `created_at DESC` (newest first)
- Internal fields (`score`, `iterations`, `token_count`) are not exposed

---

### 3. Get Task Detail

**GET** `/tasks/{task_id}`

Retrieve full details for a specific task, including score, criteria breakdown, and chat history.

#### Request

```http
GET /api/v1/bankstatement/tasks/550e8400-e29b-41d4-a716-446655440000 HTTP/1.1
Authorization: Bearer {access_token}
```

#### Response (200 OK)

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "book_id": "book_mnIDVn39cOk",
  "user_id": "keycloak-user-abc123def456",
  "status": "completed",
  "task_name": "Jan 2026 Statement",
  "file_link": "https://files.my365biz.com/files/abc123def456",
  "error": null,
  "stream_events": [
    {
      "event": "task_started",
      "timestamp": "2026-06-17T18:30:00Z"
    },
    {
      "event": "iteration_complete",
      "iteration": 1,
      "score": 8.5,
      "passed": false,
      "criteria": {
        "file_exists": 2,
        "sheet_structure": 2,
        "transaction_count": 1,
        "amount_correctness": 1.5,
        "payee_capture": 1
      },
      "issues": ["Transaction count mismatch: expected 45, got 42"],
      "strengths": ["All required columns present"]
    },
    {
      "event": "iteration_complete",
      "iteration": 2,
      "score": 12.0,
      "passed": true,
      "criteria": { ... },
      "issues": [],
      "strengths": ["Perfect transaction alignment", "All validation passed"]
    },
    {
      "event": "task_complete",
      "score": 12.0,
      "passed": true,
      "iterations": 2,
      "file_link": "https://files.my365biz.com/files/abc123def456"
    }
  ],
  "chat_history": [
    {
      "iteration": 1,
      "phase": "generator",
      "reasoning_blocks": [
        {
          "text": "Looking at the PDF structure... I can see this is a Maybank statement with transactions from Jan-Mar 2026..."
        }
      ],
      "messages": [
        {
          "role": "assistant",
          "content": "I'll start by reading the PDF structure.",
          "tool_calls": [
            {
              "id": "call_abc123",
              "type": "function",
              "function": {
                "name": "read_file",
                "arguments": "{\"path\": \"/path/to/pdf.pdf\"}"
              }
            }
          ]
        }
      ]
    }
  ],
  "created_at": "2026-06-17T18:30:00Z",
  "updated_at": "2026-06-17T18:35:00Z"
}
```

#### Scoring (0-12 Scale)

Each iteration is scored on 5 criteria (0-2 points each):

- **file_exists** (2 pts): Output Excel file created
- **sheet_structure** (2 pts): All required sheets present (Transactions, By Payee & Buyer, Daily Summary)
- **transaction_count** (2 pts): Transaction row count matches PDF
- **amount_correctness** (2 pts): Debit/Credit totals verified
- **payee_capture** (2 pts): Payee and buyer fields populated

**Pass threshold**: 12.0 (score must be ≥ 12 to avoid re-iteration)

#### Errors

| Code | Message | Reason |
|------|---------|--------|
| 401 | Unauthorized | Invalid token |
| 403 | Access denied | Task belongs to a book user doesn't have access to |
| 404 | Task not found | Task ID doesn't exist |

#### Notes

- **Internal fields hidden**: `score`, `iterations`, `token_count` (used internally only)
- **chat_history**: Full conversation with Claude (thinking blocks, tool calls, outputs) — useful for debugging
- **stream_events**: Telemetry for progress tracking (see SSE endpoint for real-time)

---

### 4. Stream Task Progress (Server-Sent Events)

**GET** `/tasks/{task_id}/stream`

Real-time progress stream using Server-Sent Events (SSE). Polls the task's event queue and emits new events as they occur.

#### Request

```http
GET /api/v1/bankstatement/tasks/550e8400-e29b-41d4-a716-446655440000/stream?token={access_token} HTTP/1.1
Accept: text/event-stream
```

**Note**: Token passed as query parameter (SSE cannot set custom headers)

#### Response (200 OK, text/event-stream)

```
data: {"event":"task_started","timestamp":"2026-06-17T18:30:00Z"}

data: {"event":"iteration_start","iteration":1}

data: {"event":"iteration_complete","iteration":1,"score":8.5,"passed":false,"criteria":{...},"issues":[...],"strengths":[...]}

: heartbeat

data: {"event":"iteration_start","iteration":2}

data: {"event":"iteration_complete","iteration":2,"score":12.0,"passed":true,"criteria":{...}}

data: {"event":"task_complete","score":12.0,"passed":true,"iterations":2,"file_link":"https://files.my365biz.com/files/abc123"}
```

#### Event Schema

| Event | Fired | Payload |
|-------|-------|---------|
| `task_started` | Task processing begins | `{timestamp}` |
| `iteration_start` | Generator iteration begins | `{iteration}` |
| `iteration_complete` | Evaluator finishes scoring | `{iteration, score, passed, criteria, issues, strengths}` |
| `task_complete` | Task finished (passed) | `{score, passed, iterations, file_link}` |
| `task_failed` | Task failed after max iterations | `{error}` |
| Heartbeat | Every ~15 seconds | `: heartbeat` (colon prefix, no data) |

#### Behavior

- Polls database every 1.5 seconds
- Emits only new events (tracks cursor position)
- Closes automatically when status becomes `completed` or `failed`
- Sends heartbeat every ~15 seconds to detect stale connections
- If client connects after task already completed with zero events queued, sends synthetic terminal event

#### Errors

| Code | Message | Reason |
|------|---------|--------|
| 401 | Unauthorized | Invalid token |
| 403 | Access denied | User doesn't have access to this task's book |
| 404 | Task not found | Task ID doesn't exist |

---

### 5. Download Result Excel

**GET** `/tasks/{task_id}/download`

Redirect to the Object Server file link for downloading the processed Excel file. Only available when task status is `completed`.

#### Request

```http
GET /api/v1/bankstatement/tasks/550e8400-e29b-41d4-a716-446655440000/download HTTP/1.1
Authorization: Bearer {access_token}
```

#### Response (302 Found)

```http
Location: https://files.my365biz.com/files/abc123def456
```

Client is redirected to the Object Server URL where the file can be downloaded.

#### Errors

| Code | Message | Reason |
|------|---------|--------|
| 401 | Unauthorized | Invalid token |
| 403 | Access denied | User doesn't have access to this task's book |
| 404 | Task not found | Task ID doesn't exist |
| 404 | Excel output file not found on Object Server | Task status is `completed` but no file_link (rare) |
| 409 | Task is not completed yet | Task status is not `completed` (still `queued`, `processing`, or `failed`) |

#### Notes

- File link validated: must start with configured Object Server base URL (prevents open redirect)
- File stored on Object Server with SHA-256 deduplication
- Permanent URL — file accessible indefinitely after task completion

---

## Health Check

**GET** `/health`

System status including queue depth and current RPM.

#### Response (200 OK)

```json
{
  "status": "ok",
  "queue_length": 0,
  "rpm": 0,
  "rpm_limit": 85
}
```

---

## Rate Limiting

### MiMo API Rate Limit (RPM)

- **Limit**: 85 tasks dispatched per minute (MiMo API constraint)
- **Enforcement**: Redis-backed sliding window (60-second window, sorted set)
- **Behavior**: Tasks enqueued beyond the limit wait in Redis queue

### Queue

- **Type**: Redis List (FIFO, LPUSH + BRPOP)
- **Persistence**: Survives API restarts
- **Dispatch**: Background asyncio dispatcher respects RPM limit before spawning Docker workers

### Monitoring

Check `/health` endpoint:
```bash
curl http://localhost:8001/health
# {"status":"ok","queue_length":5,"rpm":78,"rpm_limit":85}
```

---

## Error Handling

### Standard Error Response

All errors return a JSON object with status code and detail:

```json
{
  "detail": "Invalid or expired token"
}
```

### Common Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success (GET) |
| 202 | Accepted (async POST) |
| 302 | Redirect (download) |
| 401 | Unauthorized — missing or invalid token |
| 403 | Forbidden — permission denied or no active book |
| 404 | Not found — task or book doesn't exist |
| 409 | Conflict — task not in expected state (e.g., download before completion) |
| 422 | Unprocessable entity — invalid file type |
| 502 | Bad gateway — AuthServer or Object Server unreachable |

---

## Examples

### Example 1: Upload and Monitor

```bash
# 1. Upload PDF
curl -X POST http://localhost:8001/api/v1/bankstatement/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@statement.pdf" \
  -F "task_name=Jan 2026"

# Response:
# {
#   "task_id": "550e8400-e29b-41d4-a716-446655440000",
#   "status": "queued",
#   "task_name": "Jan 2026",
#   "created_at": "2026-06-17T18:30:00Z"
# }

TASK_ID="550e8400-e29b-41d4-a716-446655440000"

# 2. Stream progress
curl -N "http://localhost:8001/api/v1/bankstatement/tasks/$TASK_ID/stream?token=$TOKEN"
# Streams events as they occur...

# 3. Check task detail
curl "http://localhost:8001/api/v1/bankstatement/tasks/$TASK_ID" \
  -H "Authorization: Bearer $TOKEN"

# 4. Download when complete
curl "http://localhost:8001/api/v1/bankstatement/tasks/$TASK_ID/download" \
  -H "Authorization: Bearer $TOKEN" \
  -L -o result.xlsx
```

### Example 2: List and Filter

```bash
# List all tasks for current user's books
curl "http://localhost:8001/api/v1/bankstatement/tasks" \
  -H "Authorization: Bearer $TOKEN"

# Pipe through jq to filter by status
curl "http://localhost:8001/api/v1/bankstatement/tasks" \
  -H "Authorization: Bearer $TOKEN" | jq '.[] | select(.status == "completed")'
```

---

## Architecture Notes

### Per-Task Isolation

- Each task runs in an isolated Docker container with:
  - Dedicated sandbox directory (`/tmp/sandbox/{task_id}/`)
  - Memory limit: 2 GB
  - CPU limit: 1 core
  - Network: host mode (reach external services)

### Multi-Iteration Loop

- **Max iterations**: 3
- **Per iteration**: Generator (extract) → Evaluator (score)
- **Exit conditions**:
  - Score ≥ 12.0 (passed)
  - All 3 iterations exhausted
- **Feedback**: If score < 12.0, Evaluator provides structured feedback to Generator for next iteration

### File Storage

- **Uploads**: Object Server (SHA-256 deduplication)
- **Results**: Object Server (permanent, publicly accessible)
- **Internal working files**: `/tmp/sandbox/{task_id}/` (ephemeral, deleted after task completion)

### Token Usage Tracking

- Recorded per task in database (`token_count` column)
- Used internally for cost analysis and monitoring
- Not exposed in API responses

---

## Troubleshooting

### Task Stuck in "processing"

- Check `/health` to see if queue is backed up
- Worker container may have crashed (check Docker logs: `docker logs bankstatement-worker-...`)
- Check `/tasks/{id}` response for any error messages

### Upload Returns 403 "No active book selected"

- User has not selected a book in the frontend
- No books are available to the user
- Contact administrator to assign books

### Download Returns 404 After Task Completes

- Rare edge case: task marked completed but Excel upload to Object Server failed
- Check task detail for error messages in the response
- Retry the upload or contact support

### High Queue Depth

- More uploads than the 85 RPM limit allows
- Queue will naturally drain as RPM window clears
- Check `/health` to monitor queue length and current RPM
- Consider spreading uploads over time or increasing MiMo API RPM limit

---

## Changelog

### v1.0.0 (2026-06-17)

- Initial release
- FastAPI with async/await architecture
- Redis queue + RPM rate limiting (85 tasks/min)
- Per-task Docker isolation with sandbox filesystem
- AuthServer integration (Keycloak) for identity and permission checks
- Object Server for persistent file storage
- SSE streaming for real-time progress
- Generator → Evaluator loop (max 3 iterations, 12-point scoring)
