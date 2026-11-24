"""Real-artifact Given/When/Then BDD tests for trace-readiness skills upgrades.

Zero fakes and zero undeclared test dependencies (matches requirements-ci.txt:
jsonschema + pytest + Python stdlib only).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import textwrap
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


def test_bdd_sse_streaming_recipe_is_chunk_safe_and_bounded() -> None:
    """Given the SSE streaming recipe in 4-decorate-functions.md,
    When executing its preview extraction and bounded buffering logic across str, bytes,
    dict, object, and oversized chunks,
    Then it never accesses ev.text unsafely, leaves yielded wire chunks untouched,
    and caps buffered preview text at MAX_PREVIEW_CHARS."""
    decorate_fns = (
        ROOT / "skills" / "neatlogs-py-setup" / "references" / "4-decorate-functions.md"
    ).read_text(encoding="utf-8")

    # Guard against unsafe direct attribute access on wire chunks
    assert "ev.text" not in decorate_fns
    assert "MAX_PREVIEW_CHARS = 4096" in decorate_fns
    assert "leave yielded wire chunks untouched" in decorate_fns.lower()

    # Extract the real _preview_text helper and MAX_PREVIEW_CHARS from the markdown block
    match = re.search(
        r"(?m)^([ \t]*MAX_PREVIEW_CHARS\s*=\s*\d+\n\s*\n[ \t]*def _preview_text\(chunk\) -> str:.*?)(?=\n[ \t]*async def chat)",
        decorate_fns,
        re.DOTALL,
    )
    assert match is not None, "Could not locate _preview_text recipe in 4-decorate-functions.md"
    snippet = textwrap.dedent(match.group(1))
    namespace: dict[str, object] = {}
    exec(snippet, namespace)
    max_chars = namespace["MAX_PREVIEW_CHARS"]
    preview_text = namespace["_preview_text"]
    assert max_chars == 4096
    assert callable(preview_text)

    # Exercise all chunk shapes without raising and verify untouched wire passthrough
    wire_chunks = [
        "data: hello\n\n",
        b"bytes-chunk",
        bytearray(b"bad-\xff-utf8"),
        {"text": "dict-text"},
        {"content": "dict-content"},
        {"data": "dict-data"},
        {"data": 123},
        types.SimpleNamespace(text="obj-text"),
        types.SimpleNamespace(text=42),
        object(),
        None,
        "x" * 10_000,
    ]

    yielded: list[object] = []
    preview_parts: list[str] = []
    preview_len = 0
    for ev in wire_chunks:
        if preview_len < max_chars:
            piece = preview_text(ev)
            if piece:
                take = piece[: max_chars - preview_len]
                preview_parts.append(take)
                preview_len += len(take)
        yielded.append(ev)

    assert yielded == wire_chunks
    for orig, out in zip(wire_chunks, yielded, strict=True):
        assert out is orig
    combined = "".join(preview_parts)
    assert len(combined) == max_chars
    assert combined.startswith("data: hello\n\nbytes-chunkbad-\ufffd-utf8dict-textdict-contentdict-dataobj-text")


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
    Then --endpoint and NEATLOGS_ENDPOINT are explicitly documented so standalone CLI
    audits do not silently default to the US endpoint."""
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
        assert (
            "python -m neatlogs doctor --trace <trace_id> --endpoint https://eu.ingest.neatlogs.com --json"
            in text
        ), f"Missing EU doctor --trace example in {doc_name}"

    assert "--endpoint https://eu.ingest.neatlogs.com" in add_init
    assert "NEATLOGS_ENDPOINT" in add_init
    assert "| `NEATLOGS_ENDPOINT` |" in py_skill
