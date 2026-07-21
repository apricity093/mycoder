from miniapp.cache import BucketCache


def test_default_storage_is_isolated_but_explicit_storage_is_shared():
    first = BucketCache()
    second = BucketCache()
    first.put("token", "first")
    assert second.get("token") is None

    shared = {}
    left = BucketCache(shared)
    right = BucketCache(shared)
    left.put("shared", True)
    assert right.get("shared") is True
