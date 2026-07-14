"""多步 agent 运行时使用的轻量工作记忆。

session history 负责保存完整事件流；这个模块只保存更小的一层工作集：
当前任务摘要、最近接触的文件、文件短摘要，以及少量跨轮笔记。
这样下一轮 prompt 还能接上上一轮，但不会被整段历史塞满。
"""

import hashlib
from difflib import SequenceMatcher
from datetime import datetime
import re
import unicodedata
from pathlib import Path

from ..workspace import clip, now

WORKING_FILE_LIMIT = 8
EPISODIC_NOTE_LIMIT = 12
FILE_SUMMARY_LIMIT = 6
MIN_RETRIEVAL_SCORE = 0.8
NEAR_DUPLICATE_THRESHOLD = 0.92

# 检索层使用的“虚拟双语标签”。它们不会写回 session 或 durable Markdown，
# 因而旧记忆无需迁移；查询和记忆只要命中同一概念，就能获得跨语言信号。
BILINGUAL_SEARCH_ALIASES = {
    "project-conventions": (
        "project convention",
        "project conventions",
        "coding convention",
        "coding conventions",
        "项目约定",
        "项目规范",
        "编码规范",
        "开发规范",
    ),
    "key-decisions": (
        "decision",
        "decisions",
        "architecture decision",
        "design decision",
        "关键决策",
        "架构决策",
        "设计决策",
        "技术选型",
    ),
    "dependency-facts": (
        "dependency",
        "dependencies",
        "runtime",
        "environment",
        "依赖",
        "运行时",
        "运行环境",
        "环境配置",
    ),
    "user-preferences": (
        "preference",
        "preferences",
        "user preference",
        "用户偏好",
        "个人偏好",
        "使用习惯",
    ),
    "tool-usage": (
        "tool usage",
        "constrained tool",
        "constrained tools",
        "use tools",
        "工具使用",
        "受约束工具",
        "使用工具",
    ),
    "memory": (
        "memory",
        "durable memory",
        "working memory",
        "记忆",
        "长期记忆",
        "工作记忆",
    ),
    "encoding": ("encoding", "charset", "utf-8", "编码", "字符集"),
    "version": ("version", "版本"),
    "testing": ("test", "tests", "testing", "pytest", "测试", "用例"),
    "file-path": ("file", "path", "filename", "文件", "路径", "文件名"),
    "deploy-key": ("deploy key", "deployment key", "部署密钥", "发布密钥"),
    "color": ("color", "colour", "颜色"),
}

CONCEPT_WEIGHTS = {
    "project-conventions": 1.2,
    "key-decisions": 1.2,
    "dependency-facts": 1.0,
    "user-preferences": 1.0,
    "tool-usage": 1.2,
    "memory": 0.8,
    "encoding": 0.9,
    "version": 0.4,
    "testing": 0.8,
    "file-path": 0.3,
    "deploy-key": 1.2,
    "color": 0.3,
}

CJK_STOP_BIGRAMS = {
    "一个",
    "这个",
    "那个",
    "什么",
    "如何",
    "怎么",
    "是否",
    "已经",
    "当前",
}

DURABLE_TOPIC_DEFAULTS = {
    "project-conventions": {
        "title": "Project Conventions",
        "summary": "Stable repository conventions.",
        "tags": ["convention"],
    },
    "key-decisions": {
        "title": "Key Decisions",
        "summary": "Long-lived decisions and rationale anchors.",
        "tags": ["decision"],
    },
    "dependency-facts": {
        "title": "Dependency Facts",
        "summary": "Stable dependency and environment facts.",
        "tags": ["dependency"],
    },
    "user-preferences": {
        "title": "User Preferences",
        "summary": "Stable user preferences.",
        "tags": ["preference"],
    },
}


def default_memory_state():
    # 用一个小而结构化的状态，而不是一大段自由文本摘要。
    return {
        "working": {
            "task_summary": "",
            "recent_files": [],
        },
        "episodic_notes": [],
        "file_summaries": {},
        "task": "",
        "files": [],
        "notes": [],
        "next_note_index": 0,
    }


class DurableMemoryStore:
    def __init__(self, root):
        self.root = Path(root)
        self.index_path = self.root / "MEMORY.md"
        self.topics_dir = self.root / "topics"

    def topic_slugs(self):
        return [topic["topic"] for topic in self.load_index()]

    def load_index(self):
        if not self.index_path.exists():
            return []
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        topics = []
        current = None
        for raw in lines:
            line = raw.strip()
            match = re.match(r"- \[([^\]]+)\]\([^)]+\):\s*(.+)", line)
            if match:
                current = {
                    "topic": match.group(1).strip(),
                    "title": match.group(2).strip(),
                    "summary": "",
                    "tags": [],
                }
                topics.append(current)
                continue
            if current is None:
                continue
            summary_match = re.match(r"- summary:\s*(.+)", line)
            if summary_match:
                current["summary"] = summary_match.group(1).strip()
                continue
            tags_match = re.match(r"- tags:\s*(.+)", line)
            if tags_match:
                current["tags"] = [tag.strip() for tag in tags_match.group(1).split(",") if tag.strip()]
        return topics

    def load_topic_notes(self, topic):
        path = self.topics_dir / f"{topic}.md"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        notes = []
        capture = False
        updated_at = ""
        tags = []
        for raw in lines:
            line = raw.strip()
            if line.startswith("- tags:"):
                tags = [tag.strip() for tag in line.split(":", 1)[1].split(",") if tag.strip()]
            elif line.startswith("- updated_at:"):
                updated_at = line.split(":", 1)[1].strip()
            elif line == "## Notes":
                capture = True
            elif capture and line.startswith("- "):
                notes.append(
                    {
                        "text": line[2:].strip(),
                        "tags": tags,
                        "source": topic,
                        "created_at": updated_at or now(),
                        "kind": "durable",
                    }
                )
        return notes

    @staticmethod
    def _subject_key(text):
        text = str(text).strip()
        patterns = (
            r"^(.+?)\s+is\s+.+$",
            r"^(.+?)\s+are\s+.+$",
            r"^(.+?)\s+uses?\s+.+$",
            r"^(.+?)\s+should\s+.+$",
            r"^(.+?)是.+$",
            r"^(.+?)使用.+$",
        )
        for pattern in patterns:
            match = re.match(pattern, text, re.I)
            if match:
                subject = " ".join(_tokenize(match.group(1)))
                return subject or None
        return None

    def retrieval_candidates(self, query, limit=3):
        candidates = []
        for topic in self.load_index():
            notes = self.load_topic_notes(topic["topic"])
            for note in notes:
                enriched = dict(note)
                enriched["topic"] = topic["topic"]
                enriched["topic_title"] = topic.get("title", "")
                enriched["topic_summary"] = topic.get("summary", "")
                candidates.append(enriched)
        return _rank_memory_notes(candidates, query, limit=limit)

    def _write_index(self, topics):
        self.root.mkdir(parents=True, exist_ok=True)
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Durable Memory Index", ""]
        for topic in topics:
            lines.append(f"- [{topic['topic']}](topics/{topic['topic']}.md): {topic['title']}")
            lines.append(f"  - summary: {topic['summary']}")
            lines.append(f"  - tags: {', '.join(topic['tags'])}")
        self.index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _write_topic(self, topic, notes):
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        meta = DURABLE_TOPIC_DEFAULTS[topic]
        lines = [
            f"# {meta['title']}",
            "",
            f"- topic: {topic}",
            f"- summary: {meta['summary']}",
            f"- tags: {', '.join(meta['tags'])}",
            f"- updated_at: {now()}",
            "",
            "## Notes",
        ]
        for note in notes:
            lines.append(f"- {note}")
        (self.topics_dir / f"{topic}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def promote(self, promotions):
        if not promotions:
            return [], []
        topics = {topic["topic"]: topic for topic in self.load_index()}
        topic_notes = {slug: [note["text"] for note in self.load_topic_notes(slug)] for slug in topics}
        results = []
        superseded = []
        for topic, note_text in promotions:
            meta = DURABLE_TOPIC_DEFAULTS[topic]
            topics.setdefault(
                topic,
                {
                    "topic": topic,
                    "title": meta["title"],
                    "summary": meta["summary"],
                    "tags": list(meta["tags"]),
                },
            )
            existing = topic_notes.setdefault(topic, [])
            if note_text in existing:
                continue
            new_subject = self._subject_key(note_text)
            replaced = False
            if new_subject:
                for index, old_text in enumerate(list(existing)):
                    if self._subject_key(old_text) == new_subject:
                        superseded.append(f"{topic}: {old_text} -> {note_text}")
                        existing[index] = note_text
                        replaced = True
                        break
            if not replaced:
                existing.append(note_text)
            results.append(f"{topic}: {note_text}")
        self._write_index([topics[slug] for slug in sorted(topics)])
        for topic, notes in topic_notes.items():
            self._write_topic(topic, notes)
        return results, superseded


def _ensure_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def resolve_workspace_path(raw_path, workspace_root=None):
    path = Path(str(raw_path))
    if workspace_root is None:
        return path

    root = Path(workspace_root).resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def canonicalize_path(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None:
        return Path(str(raw_path)).as_posix()
    if workspace_root is None:
        return Path(str(raw_path)).as_posix()
    root = Path(workspace_root).resolve()
    return resolved.relative_to(root).as_posix()


def file_freshness(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def normalize_search_text(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    # 在 casefold 之前拆 camelCase / PascalCase，保留 coding 场景中的标识符边界。
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = text.casefold().replace("\\", "/")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _cjk_ngrams(text, size):
    grams = set()
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", text):
        if len(run) < size:
            continue
        grams.update(run[index : index + size] for index in range(len(run) - size + 1))
    if size == 2:
        grams.difference_update(CJK_STOP_BIGRAMS)
    return grams


def _path_terms(text):
    terms = set()
    for match in re.findall(r"(?:[a-z0-9_.-]+/)+[a-z0-9_.-]+|\b[a-z0-9_.-]+\.[a-z0-9]+\b", text):
        path = match.strip("./")
        if not path:
            continue
        terms.add(path)
        basename = path.rsplit("/", 1)[-1]
        terms.add(basename)
        if "." in basename:
            terms.add(basename.rsplit(".", 1)[0])
    return terms


def _contains_alias(text, alias):
    alias = normalize_search_text(alias)
    if not alias:
        return False
    if re.fullmatch(r"[a-z0-9 _./-]+", alias):
        alias_tokens = set(re.findall(r"[a-z0-9]+", alias))
        text_tokens = set(re.findall(r"[a-z0-9]+", text))
        return bool(alias_tokens) and alias_tokens <= text_tokens
    return alias in text


def _field_matches_query(query_text, field_text):
    field_text = normalize_search_text(field_text)
    if not field_text:
        return False
    if re.fullmatch(r"[a-z0-9 _./-]+", field_text):
        field_tokens = set(re.findall(r"[a-z0-9]+", field_text))
        query_tokens = set(re.findall(r"[a-z0-9]+", query_text))
        return bool(field_tokens) and field_tokens <= query_tokens
    return field_text in query_text


def search_features(text):
    normalized = normalize_search_text(text)
    ascii_tokens = set(re.findall(r"[a-z0-9]+", normalized))
    identifiers = {
        token
        for token in re.findall(r"[a-z][a-z0-9_]*", normalized)
        if "_" in token or any(char.isdigit() for char in token)
    }
    concepts = {
        concept
        for concept, aliases in BILINGUAL_SEARCH_ALIASES.items()
        if any(_contains_alias(normalized, alias) for alias in aliases)
    }
    return {
        "normalized": normalized,
        "ascii_tokens": ascii_tokens,
        "identifiers": identifiers,
        "cjk_unigrams": set(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", normalized)),
        "cjk_bigrams": _cjk_ngrams(normalized, 2),
        "cjk_trigrams": _cjk_ngrams(normalized, 3),
        "paths": _path_terms(normalized),
        "concepts": concepts,
    }


def retrieval_query_metadata(query):
    features = search_features(query)
    return {
        "normalized": features["normalized"],
        "ascii_token_count": len(features["ascii_tokens"]),
        "identifier_count": len(features["identifiers"]),
        "cjk_bigram_count": len(features["cjk_bigrams"]),
        "cjk_trigram_count": len(features["cjk_trigrams"]),
        "path_count": len(features["paths"]),
        "concepts": sorted(features["concepts"]),
    }


def _tokenize(text):
    # 保留旧内部入口的语义，但让 durable subject key 同样支持 NFKC 和中文。
    features = search_features(text)
    return features["ascii_tokens"] | features["cjk_bigrams"] | features["concepts"]


def _dice(left, right):
    left = set(left)
    right = set(right)
    if not left or not right:
        return 0.0
    return 2.0 * len(left & right) / (len(left) + len(right))


def _field_score(query_features, field_features, weight, label, signals):
    score = 0.0
    ascii_score = _dice(query_features["ascii_tokens"], field_features["ascii_tokens"])
    if ascii_score:
        value = weight * ascii_score
        score += value
        signals.append(f"{label}:ascii={value:.3f}")
    bigram_score = _dice(query_features["cjk_bigrams"], field_features["cjk_bigrams"])
    if bigram_score:
        value = weight * bigram_score
        score += value
        signals.append(f"{label}:cjk_bigram={value:.3f}")
    trigram_score = _dice(query_features["cjk_trigrams"], field_features["cjk_trigrams"])
    if trigram_score:
        value = weight * 0.65 * trigram_score
        score += value
        signals.append(f"{label}:cjk_trigram={value:.3f}")
    identifier_score = _dice(query_features["identifiers"], field_features["identifiers"])
    if identifier_score:
        value = weight * 0.75 * identifier_score
        score += value
        signals.append(f"{label}:identifier={value:.3f}")
    return score


def _topic_search_text(note):
    topic = str(note.get("topic", "")).strip()
    aliases = BILINGUAL_SEARCH_ALIASES.get(topic, ())
    return " ".join(
        [
            topic,
            str(note.get("topic_title", "")),
            str(note.get("topic_summary", "")),
            *aliases,
        ]
    )


def _score_memory_note(query_features, note):
    text_features = search_features(note.get("text", ""))
    tag_text = " ".join(str(tag) for tag in note.get("tags", []))
    tag_features = search_features(tag_text)
    source_features = search_features(note.get("source", ""))
    topic_features = search_features(_topic_search_text(note))
    signals = []
    score = 0.0

    query_text = query_features["normalized"]
    note_text = text_features["normalized"]
    if min(len(query_text), len(note_text)) >= 4 and (query_text in note_text or note_text in query_text):
        score += 3.0
        signals.append("text:phrase=3.000")

    for tag in note.get("tags", []):
        normalized_tag = normalize_search_text(tag)
        if _field_matches_query(query_text, normalized_tag):
            score += 6.0
            signals.append(f"tag:exact={normalized_tag}")
            break

    source_text = source_features["normalized"]
    if source_text and source_text in query_text:
        score += 8.0
        signals.append(f"source:exact={source_text}")
    elif query_features["paths"] & source_features["paths"]:
        score += 6.0
        signals.append("source:path=6.000")

    concept_overlap = query_features["concepts"] & text_features["concepts"]
    if concept_overlap:
        value = min(3.0, sum(CONCEPT_WEIGHTS.get(concept, 0.5) for concept in concept_overlap))
        score += value
        signals.append(f"text:concept={','.join(sorted(concept_overlap))}:{value:.3f}")
    tag_concepts = query_features["concepts"] & tag_features["concepts"]
    if tag_concepts:
        score += 5.0
        signals.append(f"tag:concept={','.join(sorted(tag_concepts))}:5.000")
    topic_concepts = query_features["concepts"] & topic_features["concepts"]
    if topic_concepts:
        score += 5.0
        signals.append(f"topic:alias={','.join(sorted(topic_concepts))}:5.000")

    score += _field_score(query_features, text_features, 3.0, "text", signals)
    score += _field_score(query_features, tag_features, 4.0, "tag", signals)
    score += _field_score(query_features, source_features, 4.0, "source", signals)

    return score, signals


def _polarity_markers(text):
    normalized = normalize_search_text(text)
    markers = (" not ", "never", "must not", "不要", "不能", "禁止", "不应")
    return {marker.strip() for marker in markers if marker in f" {normalized} "}


def _text_similarity(left, right):
    left = normalize_search_text(left)
    right = normalize_search_text(right)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _near_duplicate(left, right):
    left_text = str(left.get("text", ""))
    right_text = str(right.get("text", ""))
    if normalize_search_text(left_text) == normalize_search_text(right_text):
        return True
    if set(re.findall(r"\d+(?:\.\d+)*", left_text)) != set(re.findall(r"\d+(?:\.\d+)*", right_text)):
        return False
    if _polarity_markers(left_text) != _polarity_markers(right_text):
        return False
    return _text_similarity(left_text, right_text) >= NEAR_DUPLICATE_THRESHOLD


def _rank_memory_notes(notes, query, limit=3):
    query_features = search_features(query)
    ranked = []
    for note in notes:
        score, signals = _score_memory_note(query_features, note)
        if score < MIN_RETRIEVAL_SCORE:
            continue
        candidate = dict(note)
        candidate["retrieval_score"] = round(score, 6)
        candidate["retrieval_signals"] = signals
        ranked.append(candidate)

    ranked.sort(
        key=lambda note: (
            float(note.get("retrieval_score", 0.0)),
            _parse_timestamp(note.get("created_at")),
            int(note.get("note_index", -1)),
        ),
        reverse=True,
    )

    deduped = []
    for candidate in ranked:
        if any(_near_duplicate(candidate, selected) for selected in deduped):
            continue
        deduped.append(candidate)

    selected = []
    remaining = list(deduped)
    while remaining and len(selected) < max(0, int(limit)):
        best = None
        best_key = None
        for candidate in remaining:
            similarity = max(
                [_text_similarity(candidate.get("text", ""), item.get("text", "")) for item in selected],
                default=0.0,
            )
            source = str(candidate.get("source", "")).strip()
            same_source = bool(source) and any(source == str(item.get("source", "")).strip() for item in selected)
            topic = str(candidate.get("topic", "")).strip()
            same_topic = bool(topic) and any(topic == str(item.get("topic", "")).strip() for item in selected)
            diversity_penalty = similarity * 0.75 + (0.35 if same_source else 0.0) + (0.2 if same_topic else 0.0)
            adjusted = float(candidate["retrieval_score"]) - diversity_penalty
            key = (
                adjusted,
                float(candidate["retrieval_score"]),
                _parse_timestamp(candidate.get("created_at")),
                int(candidate.get("note_index", -1)),
            )
            if best_key is None or key > best_key:
                best = candidate
                best_key = key
        selected.append(best)
        remaining.remove(best)

    for index, candidate in enumerate(selected, start=1):
        candidate["retrieval_rank"] = index
    return selected


def _parse_timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0


def _normalize_note(note, index):
    if isinstance(note, str):
        text = clip(note.strip(), 500)
        return {
            "text": text,
            "tags": [],
            "source": "",
            "created_at": now(),
            "note_index": index,
            "kind": "episodic",
        }

    if not isinstance(note, dict):
        text = clip(str(note).strip(), 500)
        return {
            "text": text,
            "tags": [],
            "source": "",
            "created_at": now(),
            "note_index": index,
            "kind": "episodic",
        }

    text = clip(str(note.get("text", "")).strip(), 500)
    tags = [str(tag).strip() for tag in _ensure_list(note.get("tags", [])) if str(tag).strip()]
    source = str(note.get("source", "")).strip()
    created_at = str(note.get("created_at", "")).strip() or now()
    note_index = int(note.get("note_index", index))
    kind = str(note.get("kind", "episodic")).strip() or "episodic"
    return {
        "text": text,
        "tags": _dedupe_preserve_order(tags),
        "source": source,
        "created_at": created_at,
        "note_index": note_index,
        "kind": kind,
    }


def normalize_memory_state(state, workspace_root=None):
    if state is None:
        state = default_memory_state()
    elif not isinstance(state, dict):
        raise TypeError("memory state must be a mapping")

    # 规范化层的作用，是把“磁盘里可能长得不太一样的旧状态”
    # 统一整理成当前 runtime 可直接使用的紧凑结构。
    working = state.get("working")
    if not isinstance(working, dict):
        working = {}
    working.setdefault("task_summary", "")
    working.setdefault("recent_files", [])
    working["task_summary"] = clip(str(working.get("task_summary", "")).strip(), 300)
    working["recent_files"] = _dedupe_preserve_order(
        [
            canonicalize_path(path, workspace_root)
            for path in _ensure_list(working.get("recent_files", []))
            if str(path).strip()
        ]
    )[-WORKING_FILE_LIMIT:]
    state["working"] = working

    if not str(working["task_summary"]).strip() and state.get("task"):
        working["task_summary"] = clip(str(state.get("task", "")).strip(), 300)
    if not working["recent_files"] and state.get("files"):
        working["recent_files"] = _dedupe_preserve_order(
            [
                canonicalize_path(path, workspace_root)
                for path in _ensure_list(state.get("files", []))
                if str(path).strip()
            ]
        )[-WORKING_FILE_LIMIT:]

    episodic_notes = state.get("episodic_notes")
    if not isinstance(episodic_notes, list):
        episodic_notes = []

    if not episodic_notes and state.get("notes"):
        episodic_notes = [
            _normalize_note(note, index)
            for index, note in enumerate(_ensure_list(state.get("notes", [])))
            if str(note).strip()
        ]
    else:
        normalized_notes = []
        for index, note in enumerate(episodic_notes):
            if isinstance(note, str) and not str(note).strip():
                continue
            normalized_notes.append(_normalize_note(note, index))
        episodic_notes = normalized_notes
    episodic_notes = episodic_notes[-EPISODIC_NOTE_LIMIT:]
    state["episodic_notes"] = episodic_notes

    file_summaries = state.get("file_summaries")
    if not isinstance(file_summaries, dict):
        file_summaries = {}
    normalized_file_summaries = {}
    for path, summary in file_summaries.items():
        path = canonicalize_path(path, workspace_root)
        if isinstance(summary, dict):
            text = clip(str(summary.get("summary", "")).strip(), 500)
            created_at = str(summary.get("created_at", "")).strip() or now()
            freshness = summary.get("freshness")
            freshness = None if freshness in (None, "") else str(freshness).strip() or None
        else:
            text = clip(str(summary).strip(), 500)
            created_at = now()
            freshness = None
        if not path or not text:
            continue
        normalized_file_summaries[path] = {
            "summary": text,
            "created_at": created_at,
            "freshness": freshness,
        }
    state["file_summaries"] = normalized_file_summaries

    next_note_index = state.get("next_note_index")
    if not isinstance(next_note_index, int) or next_note_index < 0:
        next_note_index = 0
    max_index = max([note["note_index"] for note in episodic_notes], default=-1)
    state["next_note_index"] = max(next_note_index, max_index + 1)

    state["task"] = working["task_summary"]
    state["files"] = list(working["recent_files"])
    state["notes"] = [note["text"] for note in episodic_notes]
    durable_root = Path(workspace_root) / ".pico" / "memory" if workspace_root is not None else None
    durable_store = DurableMemoryStore(durable_root) if durable_root is not None else None
    state["durable_topics"] = durable_store.topic_slugs() if durable_store is not None else []
    return state


def set_task_summary(state, summary, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    state["working"]["task_summary"] = clip(str(summary).strip(), 300)
    state["task"] = state["working"]["task_summary"]
    return state


def remember_file(state, path, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    if not path:
        return state
    files = [item for item in state["working"]["recent_files"] if item != path]
    files.append(path)
    state["working"]["recent_files"] = files[-WORKING_FILE_LIMIT:]
    state["files"] = list(state["working"]["recent_files"])
    return state


def append_note(state, text, tags=(), source="", created_at=None, workspace_root=None, kind="episodic"):
    state = normalize_memory_state(state, workspace_root)
    text = clip(str(text).strip(), 500)
    if not text:
        return state

    normalized_tags = _dedupe_preserve_order(
        [str(tag).strip() for tag in _ensure_list(tags) if str(tag).strip()]
    )
    note = {
        "text": text,
        "tags": normalized_tags,
        "source": str(source).strip(),
        "created_at": str(created_at).strip() if created_at else now(),
        "note_index": int(state.get("next_note_index", 0)),
        "kind": str(kind).strip() or "episodic",
    }
    state["next_note_index"] = note["note_index"] + 1

    notes = [item for item in state["episodic_notes"] if item["text"] != note["text"]]
    notes.append(note)
    state["episodic_notes"] = notes[-EPISODIC_NOTE_LIMIT:]
    state["notes"] = [item["text"] for item in state["episodic_notes"]]
    return state
def set_file_summary(state, path, summary, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    summary = clip(str(summary).strip(), 500)
    if not path or not summary:
        return state
    state["file_summaries"][path] = {
        "summary": summary,
        "created_at": now(),
        "freshness": file_freshness(path, workspace_root),
    }
    return state


def invalidate_file_summary(state, path, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    if not path:
        return state
    state["file_summaries"].pop(path, None)
    return state


def invalidate_stale_file_summaries(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    invalidated = []
    for path, summary in list(state["file_summaries"].items()):
        current_freshness = file_freshness(path, workspace_root)
        if summary.get("freshness") == current_freshness:
            continue
        invalidated.append(path)
        state["file_summaries"].pop(path, None)
    return state, invalidated


def summarize_read_result(result, limit=180):
    # 我们不会把完整文件内容塞进记忆层，
    # 这里只保留足够提醒下一轮“刚刚读到了什么”的短摘要。
    lines = [line.strip() for line in str(result).splitlines() if line.strip()]
    if not lines:
        return "(empty)"
    if lines[0].startswith("# "):
        lines = lines[1:]
    if not lines:
        return "(empty)"
    summary = " | ".join(lines[:3])
    return clip(summary, limit)


def retrieval_candidates(state, query, limit=3, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    candidates = [dict(note) for note in state["episodic_notes"]]

    if workspace_root is not None:
        durable_store = DurableMemoryStore(Path(workspace_root) / ".pico" / "memory")
        for topic in durable_store.load_index():
            for note in durable_store.load_topic_notes(topic["topic"]):
                enriched = dict(note)
                enriched["topic"] = topic["topic"]
                enriched["topic_title"] = topic.get("title", "")
                enriched["topic_summary"] = topic.get("summary", "")
                candidates.append(enriched)

    return _rank_memory_notes(candidates, query, limit=limit)


def retrieval_view(state, query, limit=3, workspace_root=None):
    candidates = retrieval_candidates(state, query, limit=limit, workspace_root=workspace_root)
    lines = ["Relevant memory:"]
    if not candidates:
        lines.append("- none")
        return "\n".join(lines)
    for note in candidates:
        lines.append(f"- {note['text']}")
    return "\n".join(lines)


def render_memory_text(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    # 这里渲染的是给模型看的紧凑“仪表盘”，不是完整回放。
    # 笔记正文默认不展开，只有在相关召回时才按需拿出来。
    lines = [
        "Memory:",
        f"- task: {state['working']['task_summary'] or '-'}",
        f"- recent_files: {', '.join(state['working']['recent_files']) or '-'}",
    ]

    summaries = []
    for path in state["working"]["recent_files"][:FILE_SUMMARY_LIMIT]:
        summary = state["file_summaries"].get(path, {})
        current_freshness = file_freshness(path, workspace_root)
        if summary.get("summary", "") and summary.get("freshness") == current_freshness:
            summaries.append(f"- {path}: {summary['summary']}")
    if summaries:
        lines.append("- file_summaries:")
        lines.extend(f"  {line}" for line in summaries)
    else:
        lines.append("- file_summaries: -")

    lines.append(f"- episodic_notes: {len(state['episodic_notes'])}")
    durable_topics = state.get("durable_topics", [])
    lines.append(f"- durable_topics: {', '.join(durable_topics) or '-'}")
    return "\n".join(lines)


def is_effectively_empty(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    return (
        not str(state["working"]["task_summary"]).strip()
        and not state["working"]["recent_files"]
        and not state["episodic_notes"]
        and not state["file_summaries"]
    )


class LayeredMemory:
    def __init__(self, state=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.state = normalize_memory_state(state, workspace_root)
        self.durable_store = DurableMemoryStore(Path(workspace_root) / ".pico" / "memory") if workspace_root is not None else None

    def to_dict(self):
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return self.state

    def canonical_path(self, path):
        return canonicalize_path(path, self.workspace_root)

    def set_task_summary(self, summary):
        self.state = set_task_summary(self.state, summary, self.workspace_root)
        return self

    def remember_file(self, path):
        self.state = remember_file(self.state, path, self.workspace_root)
        return self

    def append_note(self, text, tags=(), source="", created_at=None, kind="episodic"):
        self.state = append_note(
            self.state,
            text,
            tags=tags,
            source=source,
            created_at=created_at,
            workspace_root=self.workspace_root,
            kind=kind,
        )
        return self

    def set_file_summary(self, path, summary):
        self.state = set_file_summary(self.state, path, summary, self.workspace_root)
        return self

    def invalidate_file_summary(self, path):
        self.state = invalidate_file_summary(self.state, path, self.workspace_root)
        return self

    def invalidate_stale_file_summaries(self):
        self.state, invalidated = invalidate_stale_file_summaries(self.state, self.workspace_root)
        return invalidated

    def retrieval_candidates(self, query, limit=3):
        return retrieval_candidates(self.state, query, limit=limit, workspace_root=self.workspace_root)

    def retrieval_view(self, query, limit=3):
        return retrieval_view(self.state, query, limit=limit, workspace_root=self.workspace_root)

    def render_memory_text(self):
        return render_memory_text(self.state, self.workspace_root)

    def promote_durable(self, promotions):
        if self.durable_store is None:
            return [], []
        self.state = normalize_memory_state(self.state, self.workspace_root)
        promoted, superseded = self.durable_store.promote(promotions)
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return promoted, superseded
