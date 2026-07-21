from miniapp.history import latest_event


def test_latest_event_uses_timestamp_without_mutating_input():
    events = [
        {"timestamp": "2026-07-19T10:00:00+08:00", "name": "middle"},
        {"timestamp": "2026-07-19T11:00:00+08:00", "name": "new"},
        {"timestamp": "2026-07-19T09:00:00+08:00", "name": "old"},
    ]
    original = list(events)
    assert latest_event(events)["name"] == "new"
    assert events == original
    assert latest_event([]) is None
