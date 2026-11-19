from __future__ import annotations

from lamarck.sandbox.image import FORBIDDEN_NAMES, REPO, context_files


def test_image_context_contains_no_tasks_gold_or_store_data() -> None:
    files = [str(f.relative_to(REPO)) for f in context_files()]
    assert files, "empty build context"
    for f in files:
        name = f.rsplit("/", 1)[-1].lower()
        assert not any(bad in name for bad in FORBIDDEN_NAMES), f
        assert not f.startswith(("runs/", "results/", "data/"))
    assert "lamarck/sandbox/runner.py" in files
