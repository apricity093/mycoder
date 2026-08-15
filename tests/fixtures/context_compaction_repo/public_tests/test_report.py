def test_report_deterministic():
    import json
    from report import write
    payload = {"b": 1, "a": 2}
    assert write(payload) == json.dumps(payload, sort_keys=True)
