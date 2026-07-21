from miniapp.users import unique_usernames


def test_unique_usernames_is_stable_and_unicode_aware():
    values = [" Alice ", "alice", "Straße", "STRASSE", "", " Bob", "bob "]
    assert unique_usernames(values) == ["Alice", "Straße", "Bob"]
