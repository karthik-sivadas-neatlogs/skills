"""Real-artifact Given/When/Then BDD tests for trace-readiness skills upgrades.

Zero fakes and zero undeclared test dependencies (matches requirements-ci.txt:
jsonschema + pytest + Python stdlib only).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import re
import subprocess
import sys
import textwrap
import tracemalloc
import types
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_skills_bdd", ROOT / "scripts" / "validate-skills.py"
)
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def test_bdd_skills_pass_real_validator_and_preserve_public_boundaries() -> None:
    """Given the upgraded neatlogs-py-setup and neatlogs-py skills on disk,
    When scripts/validate-skills.py runs as a real subprocess,
    Then validation exits 0 with zero forbidden internal terms or broken safety gates."""
    # When: invoke the real CI validator script
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-skills.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    # Then: exit 0 and every updated skill passes public-boundary checks
    assert proc.returncode == 0, f"validate-skills.py failed:\n{proc.stdout}\n{proc.stderr}"
    for rel in (
        "skills/neatlogs-py-setup/SKILL.md",
        "skills/neatlogs-py-setup/references/2-add-init.md",
        "skills/neatlogs-py-setup/references/understand-first.md",
        "skills/neatlogs-py-setup/references/4-decorate-functions.md",
        "skills/neatlogs-py-setup/references/6-decorate-tools.md",
        "skills/neatlogs-py-setup/references/6b-retrievers-and-provenance.md",
        "skills/neatlogs-py/SKILL.md",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        errors: list[str] = []
        VALIDATOR.validate_public_doctor_boundary(text, errors, rel)
        assert errors == [], f"Public boundary violation in {rel}: {errors}"


def test_bdd_setup_skill_encodes_judge_and_backtrack_readiness_rules() -> None:
    """Given a developer following neatlogs-py-setup,
    When reading SKILL.md and its references,
    Then root I/O, retriever freshness, session hygiene, provenance, replay=True,
    and python -m neatlogs doctor --trace are all explicitly specified and linked."""
    setup_dir = ROOT / "skills" / "neatlogs-py-setup"
    skill_md = (setup_dir / "SKILL.md").read_text(encoding="utf-8")
    understand = (setup_dir / "references" / "understand-first.md").read_text(encoding="utf-8")
    decorate_fns = (setup_dir / "references" / "4-decorate-functions.md").read_text(encoding="utf-8")
    decorate_tools = (setup_dir / "references" / "6-decorate-tools.md").read_text(encoding="utf-8")
    step_6b = (setup_dir / "references" / "6b-retrievers-and-provenance.md").read_text(
        encoding="utf-8"
    )
    py_skill = (ROOT / "skills" / "neatlogs-py" / "SKILL.md").read_text(encoding="utf-8")

    # Then: Step 6b is wired into SKILL.md and all local links resolve cleanly
    assert "references/6b-retrievers-and-provenance.md" in skill_md
    assert "python -m neatlogs doctor --trace" in skill_md
    assert "python -m neatlogs doctor --trace" in py_skill
    assert "never pass `trace_id`" in skill_md.lower() or "never pass `trace_id`" in step_6b.lower()
    assert "per-request `neatlogs.flush()`" in skill_md

    # Phase 2 inventory hunts for RAG, sessions, provenance, and streaming boundaries
    for expected in ("updated_at", "session_id", "prompt_version", "StreamingResponse"):
        assert expected in understand

    # Step 4 encodes Clean Root I/O + streaming accumulation + prompt_version
    for expected in (
        'set_attribute("input.value"',
        'set_attribute("output.value"',
        "prompt_version",
        "6b-retrievers-and-provenance.md",
    ):
        assert expected in decorate_fns

    # Step 6 encodes replay=True and links to 6b
    assert "replay=True" in decorate_tools
    assert "(6b-retrievers-and-provenance.md)" in decorate_tools

    # Step 6b encodes retrievers, freshness, sessions, provenance, checkpoints, diagnostics
    for expected in (
        'kind="RETRIEVER"',
        'kind="RERANKER"',
        'kind="GUARDRAIL"',
        "updated_at",
        "replay=True",
        "NEATLOGS_ENVIRONMENT",
        "NEATLOGS_RELEASE",
        "neatlogs.checkpoint",
        "neatlogs.report_diagnostic",
    ):
        assert expected in step_6b

    # neatlogs-py SKILL.md documents init provenance, replayable kinds, and audit CLI
    for expected in (
        "`environment`",
        "`release`",
        "NEATLOGS_ENVIRONMENT",
        "NEATLOGS_RELEASE",
        "neatlogs.checkpoint",
        "neatlogs.report_diagnostic(category",
        "neatlogs >= 1.5.0",
    ):
        assert expected in py_skill

    # Guard against wrong report_diagnostic(kind=...) signature and invalid severity="warn"
    assert "report_diagnostic(kind" not in py_skill
    assert "report_diagnostic(kind" not in step_6b
    assert 'severity="warn"' not in step_6b
    assert 'severity="warning"' in step_6b

    # Explicit >= 1.5.0 version prerequisite guard wherever PR #290 APIs are documented
    for gated_doc in (skill_md, decorate_fns, decorate_tools, step_6b, py_skill, understand):
        assert ">= 1.5.0" in gated_doc

    # Every relative markdown link in modified reference files resolves on disk
    for md_file in (
        setup_dir / "SKILL.md",
        setup_dir / "references" / "understand-first.md",
        setup_dir / "references" / "4-decorate-functions.md",
        setup_dir / "references" / "6-decorate-tools.md",
        setup_dir / "references" / "6b-retrievers-and-provenance.md",
        ROOT / "skills" / "neatlogs-py" / "SKILL.md",
    ):
        for target in LINK_RE.findall(md_file.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#")):
                continue
            target_path = target.split("#", 1)[0]
            if not target_path:
                continue
            resolved = (md_file.parent / target_path).resolve()
            assert resolved.is_file(), f"Broken relative link {target!r} in {md_file}"


def test_bdd_packaged_skill_zips_are_clean_and_include_new_reference(tmp_path: Path) -> None:
    """Given the skills directory,
    When packaging neatlogs-py-setup and neatlogs-py into release zip archives,
    Then zip integrity passes and 6b-retrievers-and-provenance.md is bundled."""
    for skill_name in ("neatlogs-py-setup", "neatlogs-py"):
        skill_dir = ROOT / "skills" / skill_name
        archive_path = tmp_path / f"{skill_name}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(skill_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(ROOT / "skills"))
        with zipfile.ZipFile(archive_path, "r") as zf:
            assert zf.testzip() is None
            names = set(zf.namelist())
            assert f"{skill_name}/SKILL.md" in names
            if skill_name == "neatlogs-py-setup":
                assert f"{skill_name}/references/6b-retrievers-and-provenance.md" in names


def _load_sse_recipe_namespace(decorate_fns: str) -> dict[str, object]:
    """Extract and execute the real ✅ RIGHT SSE streaming recipe from 4-decorate-functions.md."""
    match = re.search(
        r"(?m)^[ \t]*# ✅ RIGHT — span lives inside the generator[^\n]*\n(.*?)\n[ \t]*```",
        decorate_fns,
        re.DOTALL,
    )
    assert match is not None, "Could not locate ✅ RIGHT SSE recipe in 4-decorate-functions.md"
    snippet = textwrap.dedent(match.group(1))
    # Remove the Starlette import line so the snippet runs with stdlib-only CI deps
    snippet = re.sub(
        r"(?m)^from starlette\.responses import StreamingResponse\s*$",
        "",
        snippet,
    )

    class _RecordingStreamingResponse:
        def __init__(self, body_iterator, *, media_type: str | None = None) -> None:
            self.body_iterator = body_iterator
            self.media_type = media_type

    class _RecordingSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    recorded_spans: list[_RecordingSpan] = []

    @contextlib.contextmanager
    def _trace(*_args, **_kwargs):
        span = _RecordingSpan()
        recorded_spans.append(span)
        yield span

    namespace: dict[str, object] = {
        "StreamingResponse": _RecordingStreamingResponse,
        "neatlogs": types.SimpleNamespace(trace=_trace),
        "_recorded_spans": recorded_spans,
    }
    exec(snippet, namespace)
    return namespace


def test_bdd_sse_streaming_recipe_is_sanitized_bounded_utf8_safe_and_fail_open() -> None:
    """Given the SSE streaming recipe in 4-decorate-functions.md,
    When executing the real recipe across debug/meta SSE events, PII, split UTF-8 bytes,
    an 8 MiB byte chunk, raising chunk properties, truncation, and mid-stream errors/cancellation,
    Then it filters non-assistant events, redacts PII, decodes split UTF-8 cleanly without
    8 MiB allocations, never disrupts yielded chunks, sets media_type='text/event-stream',
    and marks truncated/incomplete streams explicitly."""
    decorate_fns = (
        ROOT / "skills" / "neatlogs-py-setup" / "references" / "4-decorate-functions.md"
    ).read_text(encoding="utf-8")

    # Guard against unsafe patterns and verify required Starlette/SSE guidance
    assert "ev.text" not in decorate_fns
    assert 'getattr(chunk, "text"' not in decorate_fns
    assert "MAX_PREVIEW_CHARS = 4096" in decorate_fns
    assert 'media_type="text/event-stream"' in decorate_fns
    assert "EventSourceResponse" in decorate_fns
    assert "sse-starlette" in decorate_fns
    assert "leave yielded wire chunks untouched" in decorate_fns.lower()

    ns = _load_sse_recipe_namespace(decorate_fns)
    collector_cls = ns["_SSEPreviewCollector"]
    chat_fn = ns["chat"]
    recorded_spans = ns["_recorded_spans"]
    req = types.SimpleNamespace(
        conversation_id="conv-1",
        user_id="user-1",
        message="Help alice@example.com",
    )

    # 1. Event filtering, PII redaction, split UTF-8 ('€' = \xe2\x82\xac), and invalid UTF-8
    mut_chunk = bytearray(b"data:  bad-\xff-utf8\n\n")
    wire_chunks = [
        b"event: debug\ndata: secret debug-user@example.com\n\n",
        b": keepalive\n\n",
        b"event: meta\ndata: internal-state\n\n",
        b"event: message\ndata: Total \xe2",
        b"\x82\xac42 for bob@example.com\n\n",
        mut_chunk,
        "data: [DONE]\n\n",
    ]

    async def _run_complete_stream():
        async def _event_gen(_req):
            for item in wire_chunks:
                yield item

        ns["event_gen"] = _event_gen
        resp = await chat_fn(req)
        assert resp.media_type == "text/event-stream"
        out = [item async for item in resp.body_iterator]
        return out

    yielded = asyncio.run(_run_complete_stream())
    assert yielded == wire_chunks
    for orig, out in zip(wire_chunks, yielded, strict=True):
        assert out is orig
    # Confirm memoryview did not leave bytearray buffer locked
    mut_chunk.extend(b"")

    span = recorded_spans[-1]
    assert json.loads(span.attributes["input.value"]) == {
        "message": "Help [REDACTED_EMAIL]"
    }
    assert span.attributes["neatlogs.output_truncated"] is False
    assert span.attributes["neatlogs.stream_incomplete"] is False
    output_payload = json.loads(span.attributes["output.value"])
    assert output_payload == {"reply": "Total €42 for [REDACTED_EMAIL] bad-\ufffd-utf8"}
    assert "debug-user@example.com" not in span.attributes["output.value"]
    assert "bob@example.com" not in span.attributes["output.value"]
    assert "internal-state" not in span.attributes["output.value"]

    # 2. Dict frame filtering (for sse-starlette EventSourceResponse)
    dict_collector = collector_cls()
    dict_collector.feed({"event": "debug", "data": "leak@example.com"})
    dict_collector.feed({"event": "message", "data": "Hi carol@example.com"})
    dict_collector.feed({"data": "!"})
    dict_collector.feed({"event": "message", "data": 123})
    assert dict_collector.finish(completed=True) == "Hi [REDACTED_EMAIL]!"
    assert dict_collector.truncated is False

    # 3. 8 MiB byte chunk must NOT allocate ~8 MiB during decode and must set truncated=True
    big_chunk = b"data: " + b"x" * (8 * 1024 * 1024) + b"\n\n"
    mem_collector = collector_cls()
    tracemalloc.start()
    try:
        mem_collector.feed(big_chunk)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024, f"Expected bounded decode memory (<512 KiB), got {peak} bytes"
    assert mem_collector.truncated is True
    assert mem_collector.finish(completed=True) == "x" * 4096

    # 4. Raising chunk properties/methods must NEVER disrupt or terminate the stream
    class _HostileEvent:
        @property
        def text(self) -> str:
            raise ValueError("not a text event")

    class _HostileDict(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("dict get failed")

    hostile_chunks = [_HostileEvent(), _HostileDict(), "data: ok\n\n"]

    async def _run_hostile_stream():
        async def _event_gen(_req):
            for item in hostile_chunks:
                yield item

        ns["event_gen"] = _event_gen
        resp = await chat_fn(req)
        return [item async for item in resp.body_iterator]

    hostile_out = asyncio.run(_run_hostile_stream())
    assert len(hostile_out) == 3
    for orig, out in zip(hostile_chunks, hostile_out, strict=True):
        assert out is orig
    hostile_span = recorded_spans[-1]
    assert json.loads(hostile_span.attributes["output.value"]) == {"reply": "ok"}
    assert hostile_span.attributes["neatlogs.stream_incomplete"] is False

    # 5. Stream exceeding cap marks truncated=True on both span attribute and output.value
    async def _run_truncated_stream():
        async def _event_gen(_req):
            yield "data: " + ("a" * 5000) + "\n\n"

        ns["event_gen"] = _event_gen
        resp = await chat_fn(req)
        return [item async for item in resp.body_iterator]

    asyncio.run(_run_truncated_stream())
    trunc_span = recorded_spans[-1]
    assert trunc_span.attributes["neatlogs.output_truncated"] is True
    assert trunc_span.attributes["neatlogs.stream_incomplete"] is False
    assert json.loads(trunc_span.attributes["output.value"]) == {
        "reply": "a" * 4096,
        "truncated": True,
    }

    # 6. Mid-stream error & early cancellation mark incomplete=True and avoid recording "reply"
    async def _run_errored_stream():
        async def _event_gen(_req):
            yield "data: partial\n\n"
            raise RuntimeError("upstream reset")

        ns["event_gen"] = _event_gen
        resp = await chat_fn(req)
        seen = []
        try:
            async for item in resp.body_iterator:
                seen.append(item)
        except RuntimeError as exc:
            return seen, str(exc)
        raise AssertionError("Expected RuntimeError to propagate")

    seen_before_err, err_msg = asyncio.run(_run_errored_stream())
    assert seen_before_err == ["data: partial\n\n"]
    assert err_msg == "upstream reset"
    err_span = recorded_spans[-1]
    assert err_span.attributes["neatlogs.stream_incomplete"] is True
    assert err_span.attributes["neatlogs.output_truncated"] is False
    err_payload = json.loads(err_span.attributes["output.value"])
    assert "reply" not in err_payload
    assert err_payload == {
        "partial_reply": "partial",
        "incomplete": True,
        "truncated": False,
    }

    async def _run_cancelled_stream():
        async def _event_gen(_req):
            yield "data: first\n\n"
            yield "data: second\n\n"

        ns["event_gen"] = _event_gen
        resp = await chat_fn(req)
        first = await resp.body_iterator.__anext__()
        await resp.body_iterator.aclose()
        return first

    assert asyncio.run(_run_cancelled_stream()) == "data: first\n\n"
    cancel_span = recorded_spans[-1]
    assert cancel_span.attributes["neatlogs.stream_incomplete"] is True
    cancel_payload = json.loads(cancel_span.attributes["output.value"])
    assert "reply" not in cancel_payload
    assert cancel_payload["partial_reply"] == "first"
    assert cancel_payload["incomplete"] is True

    # 7. Actual generator survives capture AND span.set_attribute failures completely undisturbed
    class _FailingSpan:
        def set_attribute(self, _key: str, _value: object) -> None:
            raise RuntimeError("span attribute export failed")

    @contextlib.contextmanager
    def _failing_trace(*_args, **_kwargs):
        yield _FailingSpan()

    class _BadRequest:
        conversation_id = "conv-fail"
        user_id = "user-fail"

        @property
        def message(self) -> str:
            raise ValueError("request message getter failed")

    original_feed = collector_cls.feed
    original_trace = ns["neatlogs"]
    fail_chunks = [b"data: chunk-1\n\n", b"data: chunk-2\n\n"]

    def _raising_feed(_self, _chunk) -> None:
        raise ValueError("preview collector failed")

    try:
        collector_cls.feed = _raising_feed
        ns["neatlogs"] = types.SimpleNamespace(trace=_failing_trace)

        async def _run_capture_failure_stream():
            async def _event_gen(_req):
                for item in fail_chunks:
                    yield item

            ns["event_gen"] = _event_gen
            resp = await chat_fn(_BadRequest())
            assert resp.media_type == "text/event-stream"
            return [item async for item in resp.body_iterator]

        fail_out = asyncio.run(_run_capture_failure_stream())
    finally:
        collector_cls.feed = original_feed
        ns["neatlogs"] = original_trace

    assert fail_out == fail_chunks
    for orig, out in zip(fail_chunks, fail_out, strict=True):
        assert out is orig

    # 8. Oversized request input exports a bounded, PII-redacted application projection
    long_req = types.SimpleNamespace(
        conversation_id="conv-long",
        user_id="user-long",
        message="Contact secret@example.com " + ("q" * 5000),
    )

    async def _run_long_input_stream():
        async def _event_gen(_req):
            yield "data: done\n\n"

        ns["event_gen"] = _event_gen
        resp = await chat_fn(long_req)
        return [item async for item in resp.body_iterator]

    asyncio.run(_run_long_input_stream())
    long_input_span = recorded_spans[-1]
    input_payload = json.loads(long_input_span.attributes["input.value"])
    assert input_payload["truncated"] is True
    assert "secret@example.com" not in input_payload["message"]
    assert "[REDACTED_EMAIL]" in input_payload["message"]
    assert len(input_payload["message"]) <= 4096


def test_bdd_checkpoint_docs_do_not_promise_automatic_scrubbing() -> None:
    """Given the checkpoint guidance in neatlogs-py-setup and neatlogs-py,
    When checking privacy and redaction rules,
    Then automatic secret scrubbing is NOT promised, state_delta export is documented
    as requiring allowlisted/sanitized fields, and the digest-only path is preferred."""
    step_6b = (
        ROOT / "skills" / "neatlogs-py-setup" / "references" / "6b-retrievers-and-provenance.md"
    ).read_text(encoding="utf-8")
    py_skill = (ROOT / "skills" / "neatlogs-py" / "SKILL.md").read_text(encoding="utf-8")

    for doc_name, text in (
        ("6b-retrievers-and-provenance.md", step_6b),
        ("neatlogs-py/SKILL.md", py_skill),
    ):
        lowered = text.lower()
        assert "scrub secrets automatically" not in lowered, f"Stale claim in {doc_name}"
        assert "state_delta" in text
        assert "state_digest" in text
        assert "allowlisted" in lowered
        assert "digest-only" in lowered


def test_bdd_doctor_trace_examples_and_eu_notes_keep_doctor_on_trace_region() -> None:
    """Given every skill documenting neatlogs doctor --trace and EU endpoint setup,
    When reading the Doctor examples and EU notes,
    Then API-key audits use --endpoint / NEATLOGS_ENDPOINT (https://eu.ingest.neatlogs.com)
    while token-authenticated audits (NEATLOGS_TOKEN + NEATLOGS_PROJECT_ID) use
    NEATLOGS_HOST=https://eu.app.neatlogs.com rather than the ingest host."""
    setup_skill = (ROOT / "skills" / "neatlogs-py-setup" / "SKILL.md").read_text(encoding="utf-8")
    add_init = (
        ROOT / "skills" / "neatlogs-py-setup" / "references" / "2-add-init.md"
    ).read_text(encoding="utf-8")
    py_skill = (ROOT / "skills" / "neatlogs-py" / "SKILL.md").read_text(encoding="utf-8")

    for doc_name, text in (
        ("neatlogs-py-setup/SKILL.md", setup_skill),
        ("neatlogs-py/SKILL.md", py_skill),
    ):
        assert "--endpoint https://eu.ingest.neatlogs.com" in text, f"Missing EU --endpoint in {doc_name}"
        assert "NEATLOGS_ENDPOINT" in text, f"Missing NEATLOGS_ENDPOINT in {doc_name}"
        assert "NEATLOGS_HOST=https://eu.app.neatlogs.com" in text, (
            f"Missing EU NEATLOGS_HOST for token-auth doctor audit in {doc_name}"
        )
        assert (
            "python -m neatlogs doctor --trace <trace_id> --endpoint https://eu.ingest.neatlogs.com --json"
            in text
        ), f"Missing EU API-key doctor --trace example in {doc_name}"
        assert (
            "NEATLOGS_HOST=https://eu.app.neatlogs.com python -m neatlogs doctor --trace <trace_id> --json"
            in text
        ), f"Missing EU token-auth doctor --trace example in {doc_name}"

    assert "--endpoint https://eu.ingest.neatlogs.com" in add_init
    assert "NEATLOGS_ENDPOINT" in add_init
    assert "NEATLOGS_HOST=https://eu.app.neatlogs.com" in add_init
    assert "| `NEATLOGS_ENDPOINT` |" in py_skill
    assert "| `NEATLOGS_HOST` |" in py_skill
