import pytest
from pathlib import Path

from agent_postit import store
from agent_postit.search import search, recent, SearchHit


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


def _setup(root: Path) -> None:
    store.postit_create(root, "alpha", "first line\nsecond foo line\n", dir=".")
    store.postit_create(root, "beta", "no foo\nonly bar\n", dir=".")
    store.topic_create(root, "topic1", "topic desc")
    store.postit_create(root, "alpha", "topic-scope\nfoo line\n", dir="topic1")
    store.postit_create(root, "TOPIC-like", "not allowed\n", dir=".")  # name only
    # Note: `TOPIC-like` is folded to `topic-like` on disk (case-insensitive).


def test_body_match_returns_full_line(root: Path):
    _setup(root)
    hits = search(root, "foo")
    # foo appears in alpha@root (line 2 'second foo line') and topic1/alpha
    paths = {h.path for h in hits}
    assert "alpha" in paths
    assert "topic1/alpha" in paths
    a = next(h for h in hits if h.path == "alpha")
    assert any("foo" in bm.line.lower() for bm in a.body_matches)
    assert all(isinstance(bm.line_no, int) for bm in a.body_matches)


def test_scope_name_only_skips_body(root: Path):
    _setup(root)
    hits = search(root, "foo", scope="name")
    # name 'alpha' doesn't contain 'foo'
    assert all(not h.name_match or "foo" in h.name.lower() for h in hits)
    # No body matches
    assert all(h.body_matches == [] for h in hits)
    assert all(not h.body_matches for h in hits)


def test_scope_body_only_skips_name(root: Path):
    _setup(root)
    hits = search(root, "alpha", scope="body")
    # 'alpha' name itself doesn't appear in any body except maybe
    for h in hits:
        assert h.name_match is False


def test_topic_md_never_matched(root: Path):
    _setup(root)
    hits = search(root, "topic desc")
    assert all(h.path != "TOPIC" and not h.path.endswith("/TOPIC") for h in hits)
    # 'TOPIC.md' file body not searched
    assert all(h.name.lower() != "topic" for h in hits)


def test_case_insensitive_default(root: Path):
    _setup(root)
    hits = search(root, "FOO")
    assert any(h.path == "alpha" for h in hits)


def test_case_sensitive_with_negative_inline(root: Path):
    _setup(root)
    # `(?-i)foo` should be case-sensitive: only the lowercase 'foo' line
    hits_lower = search(root, "(?-i)foo")
    paths_lower = {h.path for h in hits_lower}
    assert "alpha" in paths_lower  # alpha's body has lowercase 'foo'

    hits_upper = search(root, "(?-i)FOO")
    # No note contains uppercase 'FOO' in body, and 'alpha'/'beta' are lower
    assert all(h.path != "alpha" for h in hits_upper)


def test_limit_caps(root: Path):
    _setup(root)
    all_hits = search(root, ".*", scope="name")  # every postit's name matches
    assert len(all_hits) == 4  # alpha, beta, topic1/alpha, topic-like
    cap = search(root, ".*", scope="name", limit=2)
    assert len(cap) == 2


def test_path_format_root_and_nested(root: Path):
    _setup(root)
    hits = search(root, "alpha", scope="name")
    paths = {h.path for h in hits}
    assert "alpha" in paths        # root-level note: path == name
    assert "topic1/alpha" in paths  # nested: path == dir/name


def test_recursive_default_true(root: Path):
    _setup(root)
    hits = search(root, "foo")
    assert any(h.path == "topic1/alpha" for h in hits)


def test_recursive_false_skips_nested(root: Path):
    _setup(root)
    hits = search(root, "foo", recursive=False)
    assert all(not h.path.startswith("topic1/") for h in hits)


def test_empty_body_name_match(root: Path):
    store.postit_create(root, "empty", "", dir=".")
    hits = search(root, "empt", scope="both")
    assert any(h.path == "empty" and h.name_match for h in hits)
    e = next(h for h in hits if h.path == "empty")
    assert e.body_matches == []


def test_limit_zero_returns_empty(root: Path):
    _setup(root)
    assert search(root, ".*", scope="name", limit=0) == []


# --------------------------------------------------------------------------- #
# `recent`                                                                      #
# --------------------------------------------------------------------------- #


def test_recent_top_by_mtime(root: Path):
    import os, time
    _setup(root)
    base = time.time() + 1000  # guaranteed newer than any FS default
    p_alpha = root / "alpha.md"
    p_beta = root / "beta.md"
    p_talpha = root / "topic1" / "alpha.md"
    p_topic = root / "topic-like.md"
    # alpha (oldest), topic1/alpha (mid), beta (newest), topic-like (newest-1)
    os.utime(p_alpha, (base - 1000, base - 1000))
    os.utime(p_talpha, (base - 100, base - 100))
    os.utime(p_topic, (base - 10, base - 10))
    os.utime(p_beta, (base, base))
    out = recent(root, limit=10)
    paths = [r.path for r in out]
    assert paths[0] == "beta"
    assert paths[1] == "topic-like"
    assert paths[2] == "topic1/alpha"
    assert paths[3] == "alpha"


def test_recent_scope_by_dir(root: Path):
    import os, time
    _setup(root)
    base = time.time()
    os.utime(root / "alpha.md", (base - 5, base - 5))
    os.utime(root / "topic1" / "alpha.md", (base - 1, base - 1))
    out = recent(root, limit=10, dir="topic1")
    paths = {r.path for r in out}
    # subtree under topic1 includes topic1/alpha only (no nested topics here)
    assert paths == {"topic1/alpha"}


def test_recent_default_at_root_returns_all(root: Path):
    _setup(root)
    out = recent(root)
    names = {r.path for r in out}
    # Default limit=10; we have 4 postits
    assert names == {"alpha", "beta", "topic1/alpha", "topic-like"}


def test_recent_limit_caps(root: Path):
    _setup(root)
    assert len(recent(root, limit=1)) == 1


def test_recent_tiebreaker_path(root: Path):
    import os
    _setup(root)
    t = 1234567890
    os.utime(root / "alpha.md", (t, t))
    os.utime(root / "beta.md", (t, t))
    os.utime(root / "topic-like.md", (t, t))
    os.utime(root / "topic1" / "alpha.md", (t, t))
    out = recent(root, limit=10)
    paths = [r.path for r in out]
    # Tiebreaker = path byte-order ascending
    assert paths == sorted(paths)


def test_mixed_case_suffix_on_disk_is_foreign(root: Path):
    # A hand-created file with a mixed-case `.MD` suffix on disk is treated as
    # foreign and never matched (we only ever write lowercase `.md`).
    (root / "Foreign.MD").write_text("should-not-match foo\n")
    hits = search(root, "foo")
    assert all(h.path != "Foreign" for h in hits)
    assert all(h.path.lower() != "foreign" for h in hits)


def test_topic_md_mixed_case_on_disk_is_foreign(root: Path):
    # A hand-created `Topic.md` (lowercased basename equals `topic.md` ⇒ reserved)
    # is also skipped — it is neither a postit nor searchable.
    (root / "Topic.md").write_text("should-not-match foo\n")
    hits = search(root, "foo")
    assert all(h.name.lower() != "topic" for h in hits)


def test_case_insensitive_path_in_search(root: Path):
    # Even if a note is somehow created with a mixed-case name on disk,
    # search/postit ops are case-insensitive; but our store always writes
    # lowercase, so the canonical fixture uses lowercase dir.
    store.topic_create(root, "MyTopic", "desc with foo")
    store.postit_create(root, "MyNote", "body line foo\n", dir="mytopic")
    hits = search(root, "foo")
    assert any(h.path == "mytopic/mynote" for h in hits)
    # name does not contain 'foo' — only the body matches
    h = next(h for h in hits if h.path == "mytopic/mynote")
    assert h.name_match is False
    assert any("foo" in bm.line.lower() for bm in h.body_matches)