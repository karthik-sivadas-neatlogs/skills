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
   MAX_DECODE_BYTES = MAX_PREVIEW_CHARS * 4  # cap raw bytes decoded across the stream
   ALLOWED_SSE_EVENTS = frozenset({"message", "delta", "text", "content"})
   _EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


   def _redact_pii(text: str) -> str:
       # Substitute your app's PII/secret scrubber (or configure neatlogs.init(mask=...))
       return _EMAIL_RE.sub("[REDACTED_EMAIL]", text)


   def _project_request_input(req) -> dict[str, object]:
       # Export an approved, sanitized application-level projection — never raw request bodies
       raw_message = getattr(req, "message", "")
       text = raw_message if isinstance(raw_message, str) else ""
       truncated = len(text) > MAX_PREVIEW_CHARS
       projection: dict[str, object] = {
           "message": _redact_pii(text[:MAX_PREVIEW_CHARS]),
       }
       if truncated:
           projection["truncated"] = True
       return projection


   class _SSEPreviewCollector:
       def __init__(self) -> None:
           self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
           self._bytes_remaining = MAX_DECODE_BYTES
           self._chars_remaining = MAX_DECODE_BYTES
           self._line_buf = ""
           self._event_type = "message"
           self._parts: list[str] = []
           self._preview_len = 0
           self.truncated = False

       def _append_reply(self, text: str) -> None:
           if not text:
               return
           remaining = MAX_PREVIEW_CHARS - self._preview_len
           if remaining <= 0:
               self.truncated = True
               return
           if len(text) > remaining:
               self._parts.append(text[:remaining])
               self._preview_len = MAX_PREVIEW_CHARS
               self.truncated = True
           else:
               self._parts.append(text)
               self._preview_len += len(text)

       def _consume_line(self, line: str) -> None:
           if line == "":
               self._event_type = "message"
               return
           if line.startswith(":"):
               return  # SSE comment / keepalive
           if line.startswith("event:"):
               self._event_type = line[6:].strip() or "message"
               return
           # Only capture user-facing assistant text/content events; skip debug/meta frames
           if line.startswith("data:") and self._event_type in ALLOWED_SSE_EVENTS:
               payload = line[5:]
               if payload.startswith(" "):
                   payload = payload[1:]
               if payload and payload != "[DONE]":
                   self._append_reply(payload)

       def _consume_text(self, text: str) -> None:
           self._line_buf += text
           while "\n" in self._line_buf:
               raw_line, self._line_buf = self._line_buf.split("\n", 1)
               self._consume_line(raw_line.rstrip("\r"))

       def _flush_partial_line(self) -> None:
           if self._line_buf:
               self._consume_line(self._line_buf.rstrip("\r"))
               self._line_buf = ""

       def feed(self, chunk) -> None:
           if self.truncated:
               return
           if isinstance(chunk, (bytes, bytearray)):
               if len(chunk) > self._bytes_remaining:
                   with memoryview(chunk) as mv:
                       decoded = self._decoder.decode(mv[: self._bytes_remaining], final=False)
                   self._bytes_remaining = 0
                   self._consume_text(decoded)
                   self._flush_partial_line()
                   self.truncated = True
                   return
               self._bytes_remaining -= len(chunk)
               with memoryview(chunk) as mv:
                   decoded = self._decoder.decode(mv, final=False)
               self._consume_text(decoded)
               return
           if isinstance(chunk, str):
               if len(chunk) > self._chars_remaining:
                   sliced = chunk[: self._chars_remaining]
                   self._chars_remaining = 0
                   self._consume_text(sliced)
                   self._flush_partial_line()
                   self.truncated = True
                   return
               self._chars_remaining -= len(chunk)
               self._consume_text(chunk)
               return
           # For sse-starlette EventSourceResponse dict frames (never pass raw dicts to StreamingResponse)
           if isinstance(chunk, dict):
               event_type = chunk.get("event", "message")
               if isinstance(event_type, str) and event_type in ALLOWED_SSE_EVENTS:
                   val = chunk.get("data")
                   if isinstance(val, str) and val != "[DONE]":
                       self._append_reply(val)

       def finish(self, *, completed: bool) -> str:
           if not self.truncated:
               if completed:
                   tail = self._decoder.decode(b"", final=True)
                   if tail:
                       self._consume_text(tail)
               self._flush_partial_line()
           return _redact_pii("".join(self._parts))


   async def chat(req):
       # event_gen(req) yields valid SSE-formatted str or bytes frames
       async def traced_gen():
           collector = _SSEPreviewCollector()
           completed = False
           with neatlogs.trace(
               "chat",
               kind="WORKFLOW",
               session_id=req.conversation_id,
               end_user_id=req.user_id,
               # Requires neatlogs >= 1.5.0 (omit prompt_version on v1.4.x)
               prompt_version="chat-v2",
           ) as span:
               try:
                   span.set_attribute("input.value", json.dumps(_project_request_input(req)))
               except Exception:
                   pass  # input projection / attribute errors must never disrupt the stream
               try:
                   async for ev in event_gen(req):
                       try:
                           collector.feed(ev)
                       except Exception:
                           pass  # telemetry extraction must never disrupt or terminate the stream
                       yield ev  # leave yielded wire chunks untouched
                   completed = True
               finally:
                   try:
                       preview = collector.finish(completed=completed)
                       if completed:
                           output_payload = {"reply": preview}
                           if collector.truncated:
                               output_payload["truncated"] = True
                       else:
                           output_payload = {
                               "partial_reply": preview,
                               "incomplete": True,
                               "truncated": collector.truncated,
                           }
                       span.set_attribute("neatlogs.output_truncated", collector.truncated)
                       span.set_attribute("neatlogs.stream_incomplete", not completed)
                       span.set_attribute("output.value", json.dumps(output_payload))
                   except Exception:
                       pass
       return StreamingResponse(traced_gen(), media_type="text/event-stream")
   ```

   Keep streaming instrumentation non-invasive, sanitized, and bounded:
   - **Export approved, sanitized application-level projections — never raw request text or raw wire payloads:** Project only allowlisted, bounded, PII-redacted application fields for `input.value` and extract preview text only from allowlisted user-facing assistant text/content events for `output.value` — never export raw request bodies, `event: debug`, internal/metadata SSE frames, keepalive comments, or raw wire frames verbatim. If `data:` lines carry JSON payloads, extract only the allowlisted assistant text field and run your PII/secret scrubber (or `neatlogs.init(mask=...)`) before recording attributes.
   - **Bound raw byte decoding & preserve split UTF-8:** Cap raw bytes sliced into `codecs.getincrementaldecoder("utf-8")(errors="replace")` *before* decoding (so a multi-megabyte byte chunk never allocates megabytes and multi-byte UTF-8 characters like `€` split across chunks decode cleanly).
   - **Fail-open telemetry capture & attribute writes:** Wrap all input projection, preview extraction, and `span.set_attribute(...)` calls in `try: ... except Exception: pass` and leave yielded wire chunks untouched so capture/attribute failures or raising property getters can never disrupt or terminate the application stream.
   - **Match the response class to your chunk type:** Starlette's `StreamingResponse` accepts only `str`/`bytes` chunks (passing raw `dict` frames raises `AttributeError`) and requires `media_type="text/event-stream"` for SSE. If your generator yields `dict` frames (e.g. `{"event": "message", "data": "..."}`), return `EventSourceResponse(traced_gen())` from `sse-starlette` instead or serialize them to SSE `str`/`bytes` frames.
   - **Distinguish truncated and incomplete streams:** Track normal loop completion (`completed`) and preview truncation (`truncated`), emit `neatlogs.output_truncated` and `neatlogs.stream_incomplete`, and never record a cancelled or errored mid-stream prefix as a complete final `"reply"` (prefer recording the app's existing sanitized final assembled answer when available).

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
