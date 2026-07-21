import json

from miniapp.report import write_report


def test_write_report_is_stable_and_creates_parents(tmp_path):
    output = tmp_path / "nested" / "report.json"
    write_report(output, {"z": "中文", "a": 1})

    text = output.read_text(encoding="utf-8")
    assert text == '{\n  "a": 1,\n  "z": "中文"\n}\n'
    assert json.loads(text) == {"a": 1, "z": "中文"}
