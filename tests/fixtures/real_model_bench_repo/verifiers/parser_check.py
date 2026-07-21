from miniapp.parser import parse_csv_line


def test_parse_csv_line_normalizes_fields():
    assert parse_csv_line(" alpha, beta ,,gamma ") == ["alpha", "beta", "gamma"]
    assert parse_csv_line("   ") == []
