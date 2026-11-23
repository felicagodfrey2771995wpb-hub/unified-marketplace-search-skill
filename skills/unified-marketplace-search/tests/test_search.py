import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("market_search", ROOT / "scripts/search.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_pack_queries_merge_and_partial_failure_warns(monkeypatch):
    def fake_run(query, source, limit, timeout):
        if source == "skillhub":
            raise RuntimeError("fixture failure")
        return [{"source": source, "title": "same", "identifier": "same", "url": "https://example.test/same", "description": query, "installs": 1, "score": 1}]

    monkeypatch.setattr(module, "run_source", fake_run)
    results, warnings, completed, queries = module.search(query_pack={"original_query": "原始", "queries": [{"q": "原始"}, {"q": "canonical"}]}, limit=10, timeout=1)
    assert len(results) == 1
    assert results[0]["matched_queries"] == ["canonical", "原始"]
    assert any("skillhub" in warning for warning in warnings)
    assert "skillhub" not in completed
    assert queries == ["原始", "canonical"]
