"""
MiMo LLM Client — OpenAI-compatible wrapper with Prompt Caching.

Prompt Caching (MiMo 官方支持):
  - Cache write: 免费（限时）
  - Cache read:  标准 input 价格的 1/120 (99.2% 节省)
  - 缓存对象:   system prompt + tool definitions (每轮完全一样 → 全部可缓存)
  - 机制:       在 system content block 和最后一个 tool 加 cache_control: {type: ephemeral}
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)

PLANNER_MODEL   = os.getenv("MIMO_PLANNER_MODEL",   "mimo-v2.5-pro")
EXECUTOR_MODEL  = os.getenv("MIMO_EXECUTOR_MODEL",  "mimo-v2.5")
GENERATOR_MODEL = os.getenv("MIMO_GENERATOR_MODEL", "mimo-v2.5-pro")
EVALUATOR_MODEL = os.getenv("MIMO_EVALUATOR_MODEL", "mimo-v2.5-pro")

REASONING = {
    "high":   {"type": "enabled", "budget_tokens": 30000},
    "medium": {"type": "enabled", "budget_tokens": 15000},
    "low":    {"type": "enabled", "budget_tokens": 3000},
    "none":   {"type": "disabled"},
}


def get_client() -> OpenAI:
    api_key  = os.getenv("MIMO_API_KEY")
    base_url = os.getenv("MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1")
    if not api_key:
        raise ValueError("MIMO_API_KEY not set. Check your .env file.")
    return OpenAI(api_key=api_key, base_url=base_url)


# ── Prompt Caching ────────────────────────────────────────────────────────────

def _apply_cache_control(params: dict) -> dict:
    """
    在 system prompt 和 tools 末尾注入 cache_control breakpoints。

    Anthropic / MiMo 缓存原则:
    - system prompt 和 tool definitions 在同一个 agent loop 里每轮完全一样
    - 加上 cache_control 后，第 1 轮写入缓存，第 2-N 轮以 1/120 价格读取
    - Cache write 目前免费，cache read 省 99.2%

    实现方式:
    1. system message content string → content block list，末尾加 cache_control
    2. tools 列表最后一个工具加 cache_control（缓存整个 tools 前缀）
    """
    messages = list(params.get("messages", []))
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                # 转成 content block 格式，加 cache_control
                messages[i] = {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            elif isinstance(content, list) and content:
                # 已经是 block 格式，在最后一个 block 加 cache_control
                last = dict(content[-1])
                last["cache_control"] = {"type": "ephemeral"}
                messages[i] = dict(msg)
                messages[i]["content"] = content[:-1] + [last]
            break  # 只处理第一个 system message

    params = dict(params)
    params["messages"] = messages

    # Tools: 在最后一个 tool 加 cache_control（缓存整个 tools 数组前缀）
    tools = params.get("tools")
    if tools:
        tools = list(tools)
        last_tool = dict(tools[-1])
        last_tool["cache_control"] = {"type": "ephemeral"}
        tools[-1] = last_tool
        params["tools"] = tools

    return params


# ── Token 追踪 ────────────────────────────────────────────────────────────────

class _Usage:
    def __init__(self, prompt_tokens=0, completion_tokens=0,
                 cache_creation_tokens=0, cache_read_tokens=0):
        self.prompt_tokens        = prompt_tokens
        self.completion_tokens    = completion_tokens
        self.cache_creation_tokens = cache_creation_tokens
        self.cache_read_tokens    = cache_read_tokens
        self.total_tokens         = prompt_tokens + completion_tokens


class _SimpleResponse:
    def __init__(self, content, tool_calls, finish_reason, model, usage=None):
        self.model   = model
        self.choices = [_SimpleChoice(content, tool_calls, finish_reason)]
        self.usage   = usage or _Usage()


class _SimpleChoice:
    def __init__(self, content, tool_calls, finish_reason):
        self.finish_reason = finish_reason or "stop"
        self.message       = _SimpleMessage(content, tool_calls)


class _SimpleMessage:
    def __init__(self, content, tool_calls):
        self.role       = "assistant"
        self.content    = content
        self.tool_calls = tool_calls


class _ToolCall:
    def __init__(self, id_, name, arguments):
        self.id       = id_
        self.type     = "function"
        self.function = _Function(name, arguments)


class _Function:
    def __init__(self, name, arguments):
        self.name      = name
        self.arguments = arguments


# ── 全局 Token 追踪器 ──────────────────────────────────────────────────────────

_token_log: list[dict] = []


def get_token_log() -> list[dict]:
    return _token_log


def reset_token_log():
    _token_log.clear()
    _call_counter.clear()


def _record_usage(label: str, prompt: int, completion: int,
                  cache_creation: int = 0, cache_read: int = 0):
    _token_log.append({
        "label":                  label,
        "prompt_tokens":          prompt,
        "completion_tokens":      completion,
        "total_tokens":           prompt + completion,
        "cache_creation_tokens":  cache_creation,
        "cache_read_tokens":      cache_read,
    })


# ── 全局 Reasoning 追踪器 ─────────────────────────────────────────────────────

_reasoning_log: list[dict] = []


def get_reasoning_log() -> list[dict]:
    return list(_reasoning_log)


def reset_reasoning_log():
    _reasoning_log.clear()


def _record_reasoning(label: str, round_num: int, reasoning: str):
    if reasoning:
        _reasoning_log.append({
            "label":     label,
            "round":     round_num,
            "reasoning": reasoning,
        })


# ── Chat 入口 ─────────────────────────────────────────────────────────────────

_call_counter: dict[str, int] = {}


def chat(
    messages: list,
    model: str,
    reasoning: str = "medium",
    max_tokens: int = 4096,
    tools: list = None,
    stream: bool = False,
    stream_label: str = "",
    **kwargs,
) -> object:
    client = get_client()

    params = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        stream=stream,
        **kwargs,
    )

    reasoning_cfg = REASONING.get(reasoning, REASONING["medium"])
    params["extra_body"] = {"thinking": reasoning_cfg}

    if tools:
        params["tools"]       = tools
        params["tool_choice"] = "auto"

    # ── Prompt Caching: inject cache_control into system + tools ──────────────
    params = _apply_cache_control(params)

    if not stream:
        return client.chat.completions.create(**params)

    label = stream_label or "unknown"
    _call_counter[label] = _call_counter.get(label, 0) + 1
    round_num = _call_counter[label]
    return _stream_and_collect(client, params, stream_label, round_num)


# ── Streaming ─────────────────────────────────────────────────────────────────

def _stream_and_collect(client: OpenAI, params: dict, label: str, round_num: int = 0):
    """
    流式调用：实时打印 reasoning + content，拼装 tool_calls，收集 usage（含缓存统计）。
    """
    full_content   = ""
    full_reasoning = ""
    tool_calls_raw = {}
    finish_reason  = None
    printed_label  = False
    in_thinking    = False

    prompt_tokens         = 0
    completion_tokens     = 0
    cache_creation_tokens = 0
    cache_read_tokens     = 0

    with client.chat.completions.create(**params) as stream:
        for chunk in stream:
            # usage chunk（通常是最后一个，choices 为空）
            if hasattr(chunk, "usage") and chunk.usage:
                u = chunk.usage
                prompt_tokens         = getattr(u, "prompt_tokens",              0) or 0
                completion_tokens     = getattr(u, "completion_tokens",          0) or 0
                # MiMo 缓存字段（与 Claude API 同名）
                cache_creation_tokens = getattr(u, "cache_creation_input_tokens", 0) or 0
                cache_read_tokens     = getattr(u, "cache_read_input_tokens",     0) or 0
                # 兼容 prompt_tokens_details（OpenAI 格式）
                if hasattr(u, "prompt_tokens_details") and u.prompt_tokens_details:
                    d = u.prompt_tokens_details
                    cache_read_tokens = cache_read_tokens or (getattr(d, "cached_tokens", 0) or 0)

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            fr    = chunk.choices[0].finish_reason
            if fr:
                finish_reason = fr

            # reasoning_content（thinking）
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                full_reasoning += rc
                if not printed_label and label:
                    print(f"\n{label}", flush=True)
                    printed_label = True
                if not in_thinking:
                    print("  \033[2m[thinking]\033[0m ", end="", flush=True)
                    in_thinking = True
                print(f"\033[2m{rc}\033[0m", end="", flush=True)

            # 普通 content
            if delta.content:
                if in_thinking:
                    print()
                    in_thinking = False
                if not printed_label and label:
                    print(f"\n{label}", flush=True)
                    printed_label = True
                print(delta.content, end="", flush=True)
                full_content += delta.content

            # tool_calls 分块拼装
            if delta.tool_calls:
                if in_thinking:
                    print()
                    in_thinking = False
                for tc_chunk in delta.tool_calls:
                    idx = tc_chunk.index
                    if idx not in tool_calls_raw:
                        tool_calls_raw[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_chunk.id:
                        tool_calls_raw[idx]["id"] = tc_chunk.id
                    if tc_chunk.function:
                        if tc_chunk.function.name:
                            tool_calls_raw[idx]["name"] += tc_chunk.function.name
                        if tc_chunk.function.arguments:
                            tool_calls_raw[idx]["arguments"] += tc_chunk.function.arguments

    if printed_label:
        print()

    _record_reasoning(label, round_num, full_reasoning)

    # 缓存命中提示
    if cache_read_tokens > 0:
        print(f"  \033[2m[Cache] ✓ {cache_read_tokens:,} tokens read from cache  "
              f"| {cache_creation_tokens:,} written\033[0m", flush=True)
    elif cache_creation_tokens > 0:
        print(f"  \033[2m[Cache] ↑ {cache_creation_tokens:,} tokens written to cache\033[0m",
              flush=True)

    if label and (prompt_tokens or completion_tokens):
        _record_usage(label, prompt_tokens, completion_tokens,
                      cache_creation_tokens, cache_read_tokens)

    tool_calls = None
    if tool_calls_raw:
        tool_calls = [
            _ToolCall(v["id"], v["name"], v["arguments"])
            for _, v in sorted(tool_calls_raw.items())
        ]

    return _SimpleResponse(
        content=full_content or None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        model=params["model"],
        usage=_Usage(prompt_tokens, completion_tokens,
                     cache_creation_tokens, cache_read_tokens),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_text(response) -> str:
    for choice in response.choices:
        msg = choice.message
        if isinstance(msg.content, str):
            return msg.content
        if isinstance(msg.content, list):
            texts = [b.get("text", "") for b in msg.content if b.get("type") == "text"]
            return "\n".join(texts)
    return ""


def extract_tool_calls(response) -> list:
    for choice in response.choices:
        tc = getattr(choice.message, "tool_calls", None)
        if tc:
            return tc
    return []


# ── Conversation log saver ────────────────────────────────────────────────────

def save_conversation_log(
    messages: list,
    agent: str,
    iteration: int,
    model: str,
    log_dir: str,
    extra: dict = None,
):
    """
    Write the full conversation (messages + per-round reasoning) to a JSON file.

    File: {log_dir}/{agent}_iter{iteration}.json
    Each message is serialised as-is; tool_calls dicts are kept verbatim.
    Reasoning blocks (one per LLM call) are attached under "reasoning_blocks".
    """
    import json
    from datetime import datetime
    from pathlib import Path

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    label_prefix = f"[{agent}]"
    reasoning_blocks = [
        r for r in get_reasoning_log()
        if r["label"].startswith(label_prefix)
    ]

    def _serialise_msg(m: dict) -> dict:
        out = {}
        for k, v in m.items():
            if v is None:
                continue
            if k == "tool_calls" and isinstance(v, list):
                out[k] = []
                for tc in v:
                    if isinstance(tc, dict):
                        out[k].append(tc)
                    else:
                        out[k].append({
                            "id":   getattr(tc, "id", ""),
                            "type": "function",
                            "function": {
                                "name":      getattr(tc.function, "name", ""),
                                "arguments": getattr(tc.function, "arguments", ""),
                            },
                        })
            elif isinstance(v, list):
                out[k] = [
                    (b if isinstance(b, dict) else {"type": "text", "text": str(b)})
                    for b in v
                ]
            else:
                out[k] = v
        return out

    payload = {
        "timestamp":        datetime.now().isoformat(timespec="seconds"),
        "agent":            agent,
        "iteration":        iteration,
        "model":            model,
        "message_count":    len(messages),
        "reasoning_blocks": reasoning_blocks,
        "messages":         [_serialise_msg(m) for m in messages],
    }
    if extra:
        payload.update(extra)

    filename = Path(log_dir) / f"{agent.lower()}_iter{iteration}.json"
    filename.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  \033[2m[Log] Saved → {filename}\033[0m", flush=True)
