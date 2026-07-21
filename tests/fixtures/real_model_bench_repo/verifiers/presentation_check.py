from miniapp.presentation import public_user_label


def test_public_user_label_reuses_normalization():
    assert public_user_label("  Ada   Lovelace ") == "@Ada Lovelace"
    assert public_user_label("   ") == "@anonymous"


def test_presentation_imports_normalizer_instead_of_copying_rules():
    import inspect
    import miniapp.presentation

    source = inspect.getsource(miniapp.presentation)
    assert "normalize_display_name" in source
