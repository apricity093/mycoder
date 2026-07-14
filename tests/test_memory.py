from pico.features.memory import LayeredMemory, normalize_search_text, search_features


def test_working_memory_tracks_summary_and_recent_files():
    memory = LayeredMemory()

    memory.set_task_summary("Investigate flaky tests")
    memory.remember_file("README.md")
    memory.remember_file("src/app.py")
    memory.remember_file("README.md")

    snapshot = memory.to_dict()

    assert snapshot["working"]["task_summary"] == "Investigate flaky tests"
    assert snapshot["working"]["recent_files"] == ["src/app.py", "README.md"]
    assert snapshot["task"] == "Investigate flaky tests"
    assert snapshot["files"] == ["src/app.py", "README.md"]


def test_episodic_notes_append_and_retrieve_deterministically():
    memory = LayeredMemory()

    memory.append_note("Exact tag note", tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    memory.append_note("Keyword overlap note about memory", created_at="2026-04-07T10:01:00+00:00")
    memory.append_note("Newest unrelated note", created_at="2026-04-07T10:02:00+00:00")
    memory.append_note("Older unrelated note", created_at="2026-04-07T09:59:00+00:00")

    snapshot = memory.to_dict()
    assert [note["text"] for note in snapshot["episodic_notes"]] == [
        "Exact tag note",
        "Keyword overlap note about memory",
        "Newest unrelated note",
        "Older unrelated note",
    ]
    assert snapshot["notes"] == [
        "Exact tag note",
        "Keyword overlap note about memory",
        "Newest unrelated note",
        "Older unrelated note",
    ]

    lines = [line for line in memory.retrieval_view("recall memory", limit=4).splitlines() if line.startswith("- ")]
    assert lines == [
        "- Exact tag note",
        "- Keyword overlap note about memory",
    ]


def test_file_summaries_use_canonical_paths_and_freshness(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    memory = LayeredMemory(workspace_root=tmp_path)

    memory.set_file_summary("./sample.txt", "sample.txt: alpha")
    memory.remember_file("./sample.txt")
    snapshot = memory.to_dict()["file_summaries"]["sample.txt"]

    assert snapshot["summary"] == "sample.txt: alpha"
    assert snapshot["freshness"]

    assert "sample.txt: alpha" in memory.render_memory_text()
    file_path.write_text("beta\n", encoding="utf-8")
    assert "sample.txt: alpha" not in memory.render_memory_text()

    memory.invalidate_file_summary("sample.txt")

    assert "sample.txt" not in memory.to_dict()["file_summaries"]


def test_process_notes_keep_kind_and_latest_duplicate_wins():
    memory = LayeredMemory()

    memory.append_note(
        "Shell partial success on README.md; inspect diff before retry",
        tags=("process", "partial_success"),
        created_at="2026-04-07T10:00:00+00:00",
        kind="process",
    )
    memory.append_note(
        "Shell partial success on README.md; inspect diff before retry",
        tags=("process", "partial_success"),
        created_at="2026-04-07T10:01:00+00:00",
        kind="process",
    )

    notes = memory.to_dict()["episodic_notes"]

    assert len(notes) == 1
    assert notes[0]["kind"] == "process"
    assert notes[0]["created_at"] == "2026-04-07T10:01:00+00:00"


def test_durable_memory_index_and_topic_notes_are_loaded_and_retrieved(tmp_path):
    memory_root = tmp_path / ".pico" / "memory"
    topics_dir = memory_root / "topics"
    topics_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project-conventions](topics/project-conventions.md): Project Conventions\n"
        "  - summary: Stable repository conventions.\n"
        "  - tags: convention\n",
        encoding="utf-8",
    )
    (topics_dir / "project-conventions.md").write_text(
        "# Project Conventions\n\n"
        "- topic: project-conventions\n"
        "- summary: Stable repository conventions.\n"
        "- tags: convention\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n"
        "- Preserve local agent state under .pico/.\n",
        encoding="utf-8",
    )

    memory = LayeredMemory(workspace_root=tmp_path)

    snapshot = memory.to_dict()
    assert snapshot["durable_topics"] == ["project-conventions"]

    lines = [line for line in memory.retrieval_view("constrained tools", limit=4).splitlines() if line.startswith("- ")]
    chinese_lines = [line for line in memory.retrieval_view("项目规范是什么？", limit=4).splitlines() if line.startswith("- ")]
    assert any("Use constrained tools instead of guessing." in line for line in lines)
    assert any("Use constrained tools instead of guessing." in line for line in chinese_lines)


def test_search_features_normalize_nfkc_identifiers_paths_and_chinese_ngrams():
    features = search_features("ＣｏｎｔｅｘｔManager 位于 pico\\context_manager.py，负责上下文预算")

    assert normalize_search_text("ＵＴＦ－８") == "utf-8"
    assert {"context", "manager", "pico", "py"} <= features["ascii_tokens"]
    assert "context_manager.py" in features["paths"]
    assert "上下" in features["cjk_bigrams"]
    assert "上下文" in features["cjk_trigrams"]


def test_chinese_memory_retrieval_matches_relevant_note_and_rejects_project_only_overlap():
    memory = LayeredMemory()
    memory.append_note("项目统一使用 UTF-8 编码。", created_at="2026-07-14T10:00:00+00:00")
    memory.append_note("项目使用 Python 3.12。", created_at="2026-07-14T10:01:00+00:00")

    selected = memory.retrieval_candidates("项目采用什么编码？", limit=3)
    unrelated = memory.retrieval_candidates("README 文件在哪里？", limit=3)

    assert [note["text"] for note in selected] == ["项目统一使用 UTF-8 编码。"]
    assert unrelated == []
    assert selected[0]["retrieval_score"] > 0
    assert any("cjk_" in signal or "concept=" in signal for signal in selected[0]["retrieval_signals"])


def test_virtual_bilingual_tags_support_cross_language_retrieval_without_schema_changes():
    memory = LayeredMemory()
    memory.append_note("Use constrained tools instead of guessing.")
    memory.append_note("运行环境使用 Python 3.12。")

    zh_selected = memory.retrieval_candidates("工具使用方面有什么要求？", limit=3)
    en_selected = memory.retrieval_candidates("What Python runtime version is used?", limit=3)

    assert zh_selected[0]["text"] == "Use constrained tools instead of guessing."
    assert en_selected[0]["text"] == "运行环境使用 Python 3.12。"
    assert "tool-usage" in " ".join(zh_selected[0]["retrieval_signals"])
    assert set(memory.to_dict()["episodic_notes"][0]) == {
        "text",
        "tags",
        "source",
        "created_at",
        "note_index",
        "kind",
    }


def test_retrieval_rejects_generic_cross_language_concept_false_positive():
    memory = LayeredMemory()
    memory.append_note("团队吉祥物是蓝色。")

    assert memory.retrieval_candidates("部署密钥是什么颜色？", limit=3) == []


def test_retrieval_dedupes_near_duplicates_but_preserves_conflicting_versions():
    memory = LayeredMemory()
    memory.append_note("Use constrained tools instead of guessing.", tags=("tools",))
    memory.append_note("Use constrained tools instead of guessing!", tags=("tools",))
    memory.append_note("Python runtime is 3.11.", tags=("runtime",))
    memory.append_note("Python runtime is 3.12.", tags=("runtime",))

    tool_notes = memory.retrieval_candidates("tools", limit=3)
    runtime_notes = memory.retrieval_candidates("runtime", limit=3)

    assert len(tool_notes) == 1
    assert {note["text"] for note in runtime_notes} == {
        "Python runtime is 3.11.",
        "Python runtime is 3.12.",
    }


def test_retrieval_diversifies_sources_when_scores_are_close():
    memory = LayeredMemory()
    memory.append_note("Recall alpha constraint.", tags=("recall",), source="a.txt")
    memory.append_note("Recall beta constraint.", tags=("recall",), source="a.txt")
    memory.append_note("Recall gamma constraint.", tags=("recall",), source="b.txt")

    selected = memory.retrieval_candidates("recall constraint", limit=2)

    assert len(selected) == 2
    assert {note["source"] for note in selected} == {"a.txt", "b.txt"}
