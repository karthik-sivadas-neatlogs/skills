# Step 4: Identify and Decorate Functions

## The One Rule That Matters Most

Ask yourself for EVERY function: **"Is this function directly triggered by the user (CLI command, HTTP endpoint, job), or is it internal?"**

- User-triggered → `@span(kind="WORKFLOW")` — this is the TRACE ROOT
- Internal orchestrator (calls other functions but no LLM call itself) → `@span(kind="CHAIN")`
- Contains an LLM API call → `@span(kind="CHAIN")`
- Discrete I/O or tool capability → `@span(kind="TOOL")` [Step 6]

## WRONG vs RIGHT — Classification

```python
# ❌ WRONG — run_pipeline is NOT a WORKFLOW. It's internal. Nobody calls it directly.
@neatlogs.span(kind="WORKFLOW")
def run_pipeline(text, settings):
    return asyncio.run(process_document(text, settings))

# ❌ WRONG — process_document is NOT a WORKFLOW. It's an internal orchestrator.
@neatlogs.span(kind="WORKFLOW")
async def process_document(text, settings):
    chunks = chunk_text(text)
    results = await asyncio.gather(...)
    return combine(results)

# ✅ RIGHT — the click command IS the user-facing entry point. THIS is the WORKFLOW.
@cli.command()
@click.argument("file_path")
@neatlogs.span(kind="WORKFLOW", name="process")
def process(file_path: str):
    text = read_document(file_path)
    result = run_pipeline(text, settings)
    write_document(result.report, output_path)

# ✅ RIGHT — internal orchestrators are CHAIN
@neatlogs.span(kind="CHAIN", name="run_pipeline")
def run_pipeline(text, settings):
    return asyncio.run(process_document(text, settings))

@neatlogs.span(kind="CHAIN", name="process_document")
async def process_document(text, settings):
    chunks = chunk_text(text)
    results = await asyncio.gather(...)
    return combine(results)
```

## Why This Matters: Trace Context Binding

All spans in one execution MUST share one trace. Spans inherit their parent's trace context. If a decorated function is called from an UNDECORATED function, it starts a NEW orphan trace.

```
# What happens WITHOUT decorating the CLI command:
process() [no span]
  ├── read_document()  → orphan trace A (lost!)
  ├── run_pipeline()   → orphan trace B
  └── write_document() → orphan trace C (lost!)

# What happens WITH the CLI command as WORKFLOW:
process() [WORKFLOW span — trace root]
  ├── read_document()  → child of process (same trace!)
  ├── run_pipeline()   → child of process (same trace!)
  │     └── process_document() → grandchild (same trace!)
  └── write_document() → child of process (same trace!)
```

## Decision Tree

**Each FEATURE is its own independent WORKFLOW = one trace root.** A service has many features —
every route handler, every CLI sub-command, every queue/cron consumer, every public library entry
point is a SEPARATE, independent workflow. They are SIBLINGS (each its own trace), never nested under
one another and never collapsed into a single app-wide workflow. The CHAIN/LLM/TOOL/RETRIEVER spans a
feature uses nest UNDER that feature's WORKFLOW root.

For each function, answer in order:

1. **Is it a user-facing entry point / a feature?** (FastAPI route, click command, celery/queue/kafka
   consumer handler, cron job, `main()`, a public library function a caller invokes directly)
   → YES → `@span(kind="WORKFLOW")` — this is an independent trace root.

   ⚠️ STREAMING / SSE entry points (the handler RETURNS a `StreamingResponse` / `EventSourceResponse` /
   a generator): do NOT use a `@span` decorator on the handler. It returns immediately, so the span
   closes BEFORE the generator runs — the streamed children then fire with no active parent and scatter
   into separate orphan traces. Open the WORKFLOW span INSIDE the generator instead:
   ```python
   # ❌ WRONG — decorator closes when the StreamingResponse is returned, orphaning the stream
   @neatlogs.span(kind="WORKFLOW", name="chat")
   async def chat(...):
       return StreamingResponse(event_gen(), media_type="text/event-stream")

   # ✅ RIGHT — span lives inside the generator, stays open across all yields → one trace
   import codecs
   import json
   import re
   from starlette.responses import StreamingResponse

   MAX_PREVIEW_CHARS = 4096
   MAX_SANITIZE_CHARS = MAX_PREVIEW_CHARS * 2  # sanitize bounded lookahead before clipping
   MAX_DECODE_BYTES = MAX_PREVIEW_CHARS * 4  # cap raw bytes decoded across the stream
   ALLOWED_SSE_EVENTS = frozenset({"message", "delta", "text", "content"})
   _EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
   _BEARER_RE = re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+\S+")
   _API_KEY_RE = re.compile(r"\b(?:sk|pk|nl[a-z]{0,2})[-_][A-Za-z0-9_-]{12,}\b")
   _TRAILING_EMAIL_FRAG_RE = re.compile(r"(?:^|(?<=\s))\S*@\S*$")
   _TRAILING_KEY_FRAG_RE = re.compile(r"\b(?:sk|pk|nl[a-z]{0,2})[-_]\S*$")
   _LINE_END_RE = re.compile(r"\r\n|\r|\n")


   def _redact_pii(text: str) -> str:
       # Substitute your app's PII/secret scrubber (or configure neatlogs.init(mask=...))
       text = _BEARER_RE.sub("[REDACTED_TOKEN]", text)
       text = _API_KEY_RE.sub("[REDACTED_SECRET]", text)
       return _EMAIL_RE.sub("[REDACTED_EMAIL]", text)


   def _sanitize_and_bound(text: str, *, already_truncated: bool = False) -> tuple[str, bool]:
       exceeded = already_truncated or len(text) > MAX_SANITIZE_CHARS
       window = text[:MAX_SANITIZE_CHARS]
       if exceeded:
           # Guard incomplete email/key fragments cut at the lookahead or collector boundary
           window = _TRAILING_EMAIL_FRAG_RE.sub("[REDACTED_EMAIL]", window)
           window = _TRAILING_KEY_FRAG_RE.sub("[REDACTED_SECRET]", window)
       sanitized = _redact_pii(window)
       if len(sanitized) > MAX_PREVIEW_CHARS:
           clipped = _TRAILING_EMAIL_FRAG_RE.sub("", sanitized[:MAX_PREVIEW_CHARS])
           return clipped, True
       return sanitized, exceeded


   def _safe_str_attr(obj, name: str) -> str | None:
       try:
           val = getattr(obj, name, None)
           return val if isinstance(val, str) and val else None
       except Exception:
           return None


   def _extract_approved_dict_text(obj: dict) -> str:
       # Every present event/type tag must be allowlisted (don't let event="message" mask type="debug")
       for tag_key in ("event", "type"):
           if tag_key in obj:
               tag = obj[tag_key]
               if not isinstance(tag, str) or tag not in ALLOWED_SSE_EVENTS:
                   return ""
       for key in ("text", "delta", "content"):
           val = obj.get(key)
           if isinstance(val, str):
               return val
           if key == "delta" and isinstance(val, dict):
               for tag_key in ("event", "type"):
                   if tag_key in val:
                       tag = val[tag_key]
                       if not isinstance(tag, str) or tag not in ALLOWED_SSE_EVENTS:
                           return ""
               nested = val.get("text") or val.get("content")
               if isinstance(nested, str):
                   return nested
       return ""


   def _extract_approved_payload_text(payload: object) -> str:
       if isinstance(payload, dict):
           return _extract_approved_dict_text(payload)
       if not isinstance(payload, str) or not payload or payload == "[DONE]":
           return ""
       if len(payload) > MAX_DECODE_BYTES:
           head = payload[:MAX_DECODE_BYTES].lstrip()
           return "" if (not head or head.startswith(("{", "["))) else payload[:MAX_SANITIZE_CHARS]
       stripped = payload.strip()
       if stripped.startswith(("{", "[")):
           try:
               parsed = json.loads(stripped)
           except ValueError:
               # Never export truncated/unparsed JSON wire objects or arrays
               return ""
           return _extract_approved_dict_text(parsed) if isinstance(parsed, dict) else ""
       return payload


   def _project_request_input(req) -> dict[str, object]:
       # Export an approved, sanitized application-level projection — never raw request bodies
       text = _safe_str_attr(req, "message") or ""
       sanitized, truncated = _sanitize_and_bound(text)
       projection: dict[str, object] = {"message": sanitized}
       if truncated:
           projection["truncated"] = True
       return projection


   class _SSEPreviewCollector:
       def __init__(self) -> None:
           self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
           self._bytes_remaining = MAX_DECODE_BYTES
           self._chars_remaining = MAX_DECODE_BYTES
           self._seen_text = False
           self._line_buf = ""
           self._event_type: str | None = None
           self._data_lines: list[str] = []
           self._parts: list[str] = []
           self._buffered_len = 0
           self.truncated = False

       def _append_reply(self, text: str) -> None:
           if not text:
               return
           remaining = MAX_SANITIZE_CHARS - self._buffered_len
           if remaining <= 0:
               self.truncated = True
               return
           if len(text) > remaining:
               self._parts.append(text[:remaining])
               self._buffered_len = MAX_SANITIZE_CHARS
               self.truncated = True
           else:
               self._parts.append(text)
               self._buffered_len += len(text)

       def _dispatch_event(self) -> None:
           event_name = self._event_type or "message"
           data_lines = self._data_lines
           self._event_type = None
           self._data_lines = []
           if event_name not in ALLOWED_SSE_EVENTS or not data_lines:
               return
           raw_payload = "\n".join(data_lines)
           extracted = _extract_approved_payload_text(raw_payload)
           if extracted:
               self._append_reply(extracted)

       def _consume_line(self, line: str) -> None:
           if line == "":
               # Per WHATWG SSE spec, dispatch only on blank-line terminator
               self._dispatch_event()
               return
           if line.startswith(":"):
               return  # SSE comment / keepalive
           if line.startswith("event:"):
               self._event_type = line[6:].strip() or None
               return
           if line.startswith("data:"):
               payload = line[5:]
               if payload.startswith(" "):
                   payload = payload[1:]
               self._data_lines.append(payload)

       def _consume_text(self, text: str, *, final: bool = False) -> None:
           if not self._seen_text and text:
               self._seen_text = True
               if text.startswith("\ufeff"):
                   text = text[1:]
           self._line_buf += text
           pos = 0
           buf = self._line_buf
           for match in _LINE_END_RE.finditer(buf):
               if match.group(0) == "\r" and match.end() == len(buf) and not final:
                   break  # wait for next chunk in case \r is split across \r\n
               self._consume_line(buf[pos : match.start()])
               pos = match.end()
           self._line_buf = buf[pos:]

       def feed(self, chunk) -> None:
           if self.truncated:
               return
           if isinstance(chunk, (bytes, bytearray, memoryview)):
               own_mv = None if isinstance(chunk, memoryview) else memoryview(chunk)
               mv = chunk if own_mv is None else own_mv
               byte_mv = mv if (mv.ndim == 1 and mv.format == "B") else mv.cast("B")
               try:
                   if len(byte_mv) > self._bytes_remaining:
                       with byte_mv[: self._bytes_remaining] as head:
                           decoded = self._decoder.decode(head, final=False)
                       self._bytes_remaining = 0
                       self._consume_text(decoded)
                       self.truncated = True
                       return
                   self._bytes_remaining -= len(byte_mv)
                   decoded = self._decoder.decode(byte_mv, final=False)
                   self._consume_text(decoded)
                   return
               finally:
                   if byte_mv is not mv:
                       byte_mv.release()
                   if own_mv is not None:
                       own_mv.release()
           if isinstance(chunk, str):
               if len(chunk) > self._chars_remaining:
                   sliced = chunk[: self._chars_remaining]
                   self._chars_remaining = 0
                   self._consume_text(sliced)
                   self.truncated = True
                   return
               self._chars_remaining -= len(chunk)
               self._consume_text(chunk)
               return
           # For sse-starlette EventSourceResponse dict frames (never pass raw dicts to StreamingResponse)
           if isinstance(chunk, dict):
               event_type = chunk.get("event", "message")
               if isinstance(event_type, str) and event_type in ALLOWED_SSE_EVENTS:
                   if "data" in chunk:
                       raw_data = chunk["data"]
                       if isinstance(raw_data, str) and len(raw_data) > MAX_DECODE_BYTES:
                           head = raw_data[:MAX_DECODE_BYTES].lstrip()
                           if not head or head.startswith(("{", "[")):
                               self.truncated = True
                               return
                           self._append_reply(raw_data[:MAX_SANITIZE_CHARS])
                           self.truncated = True
                           return
                       extracted = _extract_approved_payload_text(raw_data)
                   else:
                       extracted = _extract_approved_dict_text(chunk)
                   if extracted:
                       self._append_reply(extracted)

       def finish(self, *, completed: bool) -> str:
           if completed and not self.truncated:
               tail = self._decoder.decode(b"", final=True)
               self._consume_text(tail, final=True)
           sanitized, self.truncated = _sanitize_and_bound(
               "".join(self._parts),
               already_truncated=self.truncated,
           )
           return sanitized


   async def chat(req):
       # event_gen(req) yields valid SSE-formatted str, bytes, or memoryview frames
       # (convert upstream bytearray chunks with bytes(chunk) before yielding to StreamingResponse)
       async def traced_gen():
           collector = _SSEPreviewCollector()
           completed = False
           capture_failed = False
           input_truncated = False
           with neatlogs.trace(
               "chat",
               kind="WORKFLOW",
               session_id=_safe_str_attr(req, "conversation_id"),
               end_user_id=_safe_str_attr(req, "user_id"),
               # Requires neatlogs >= 1.5.0 (omit prompt_version on v1.4.x)
               prompt_version="chat-v2",
           ) as span:
               try:
                   input_projection = _project_request_input(req)
                   input_truncated = bool(input_projection.get("truncated"))
                   span.set_attribute("input.value", json.dumps(input_projection))
               except Exception:
                   pass  # input projection / attribute errors must never disrupt the stream
               try:
                   async for ev in event_gen(req):
                       try:
                           collector.feed(ev)
                       except Exception:
                           capture_failed = True  # telemetry errors must never disrupt the stream
                       yield ev  # leave yielded wire chunks untouched
                   completed = True
               finally:
                   try:
                       preview = collector.finish(completed=completed)
                       stream_incomplete = (not completed) or capture_failed
                       capture_truncated = input_truncated or collector.truncated
                       span.set_attribute("neatlogs.capture.truncated", capture_truncated)
                       span.set_attribute("neatlogs.stream.incomplete", stream_incomplete)
                       if not stream_incomplete:
                           output_payload: dict[str, object] = {"reply": preview}
                           if collector.truncated:
                               output_payload["truncated"] = True
                           span.set_attribute("output.value", json.dumps(output_payload))
                   except Exception:
                       pass
       return StreamingResponse(traced_gen(), media_type="text/event-stream")
   ```

   Keep streaming instrumentation non-invasive, sanitized, and bounded:
   - **Export approved, sanitized application-level projections — never raw request text or raw wire payloads:** Project only allowlisted, bounded, PII-redacted application fields for `input.value` and dispatch SSE frames only on the blank-line terminator (per WHATWG SSE spec, so `event: debug` appearing after `data:` is still filtered). For JSON/dict `data:` payloads, extract only allowlisted assistant text fields (`text`, `delta`, `content`) rather than exporting full JSON or `metadata`. Sanitize emails, bearer tokens, and API keys *before* clipping to `MAX_PREVIEW_CHARS` (and re-bound after redaction so redaction expansion still respects the cap and sets `truncated`).
   - **Bound raw byte decoding & preserve split UTF-8:** Cap raw bytes (`bytes`, `bytearray`, or `memoryview`) sliced into `codecs.getincrementaldecoder("utf-8")(errors="replace")` *before* decoding (so a multi-megabyte byte chunk never allocates megabytes and multi-byte UTF-8 characters like `€` split across chunks decode cleanly).
   - **Fail-open telemetry capture & attribute writes:** Guard identity attribute lookups (`conversation_id`/`user_id`), input projection, preview extraction, and `span.set_attribute(...)` calls in `try: ... except Exception: pass` and leave yielded wire chunks untouched so capture/attribute failures or raising property getters can never disrupt or terminate the application stream.
   - **Match the response class to your chunk type:** Starlette's `StreamingResponse` accepts only `str`, `bytes`, or `memoryview` chunks (passing raw `dict` or `bytearray` frames raises `AttributeError` — convert upstream `bytearray` chunks via `bytes(chunk)` before yielding) and requires `media_type="text/event-stream"` for SSE. If your generator yields `dict` frames (e.g. `{"event": "message", "data": "..."}`), return `EventSourceResponse(traced_gen())` from `sse-starlette` instead or serialize them to SSE `str`/`bytes` frames.
   - **Distinguish truncated and incomplete streams with canonical Doctor flags:** Track normal loop completion (`completed`), per-chunk capture errors (`capture_failed`), and input/preview truncation (`capture_truncated`), emit `neatlogs.capture.truncated` and `neatlogs.stream.incomplete`, and omit final root `output.value` completely whenever the stream is incomplete (cancelled, errored, or capture-failed) so `neatlogs doctor --trace` never counts a partial prefix as a final answer (prefer recording the app's existing sanitized final assembled answer when available).

## Clean Root I/O & Provenance Contract

Automated evaluators and step-by-step replay read the `WORKFLOW` root span first:
- **Non-empty root `input` and `output`:** Ensure the `WORKFLOW` entry point receives the user request and returns the final answer (or explicitly calls `span.set_attribute("input.value", ...)` / `span.set_attribute("output.value", ...)` for streaming handlers or functions that return `None`).
- **Prompt & checkpoint provenance (`neatlogs >= 1.5.0`):** Check installed SDK version first (`python -c "import neatlogs; print(neatlogs.__version__)"`). On `neatlogs >= 1.5.0`, pass `prompt_version="..."` on versioned `WORKFLOW` roots and use `checkpoint_id="..."` (or `neatlogs.checkpoint(...)`) on multi-step loops so traces can be backtracked cleanly (omit on `v1.4.x` to avoid `TypeError`/`AttributeError`). See [`6b-retrievers-and-provenance.md`](6b-retrievers-and-provenance.md).

2. **Does it contain a direct LLM API call?** (client.chat.completions.create, etc.)
   → YES → `@span(kind="CHAIN")` (see Step 5 for trace() wrapper)
   Exception: skip if the function ONLY does `return client.chat.completions.create(...)` with zero other logic.

3. **Does it orchestrate multiple child functions without an LLM call?**
   → YES → `@span(kind="CHAIN")`

4. **Does it do discrete I/O / external API?**
   → YES → `@span(kind="TOOL")` (handled in Step 6)

5. **Is it a pure utility?** (formatting, config, <3 lines, no I/O)
   → NO decorator

## Decorator Placement with Click/Typer

```python
# @neatlogs.span goes BELOW all click decorators, ABOVE def:
@cli.command()
@click.argument("file_path")
@click.option("--output", "-o", default=None)
@neatlogs.span(kind="WORKFLOW", name="process")
def process(file_path: str, output: str | None):
    ...
```

## Decorator Placement with @retry

```python
# @neatlogs.span goes BELOW @retry, closest to def:
@retry(stop=stop_after_attempt(3))
@neatlogs.span(kind="CHAIN", name="summarize_chunk")
async def summarize_chunk(text, settings):
    ...
```

## Action

1. Read ALL source files from the entry point outward.
2. For each CLI command / endpoint / job → add `@span(kind="WORKFLOW")`.
3. For each internal function with an LLM call → add `@span(kind="CHAIN")`.
4. For each internal orchestrator (no LLM, calls children) → add `@span(kind="CHAIN")`.
5. Add `import neatlogs` to each file that gets decorators.

## Verify BEFORE moving to step 5

1. Every CLI command / route handler has `@span(kind="WORKFLOW")`. Count them — if there are 3 commands, there must be 3 WORKFLOW spans.
2. No internal function (run_pipeline, process_document, process_chunk) has kind="WORKFLOW". These are CHAIN.
3. The WORKFLOW span is the outermost decorated function — all other spans are called beneath it.
