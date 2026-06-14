from backend.b4_gate.sample import load_sample


def test_load_sample_has_buckets():
    rows = load_sample()
    assert 450 <= len(rows) <= 550
    buckets = {r.bucket for r in rows}
    assert "bse" in buckets
    assert "main_mature" in buckets
