import os

import pytest
from pathlib import Path

from agent_postit import store
from agent_postit.store import StoreError


# --------------------------------------------------------------------------- #
# fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "postit-root"
    r.mkdir()
    return r


def _topic(root: Path, dir: str, description: str = "desc") -> None:
    store.topic_create(root, dir, description)


# --------------------------------------------------------------------------- #
# topic.create                                                                #
# --------------------------------------------------------------------------- #


class TestTopicCreate:
    def test_creates_dir_and_topic_file(self, root: Path):
        info = store.topic_create(root, "t1", "desc")
        assert (root / "t1").is_dir()
        assert (root / "t1" / "TOPIC.md").is_file()
        assert info.description == "desc"
        assert info.dir == "t1"

    def test_empty_description_allowed(self, root: Path):
        info = store.topic_create(root, "t1", "")
        assert (root / "t1" / "TOPIC.md").read_text() == ""
        assert info.size == 0

    def test_duplicate_dir_exists(self, root: Path):
        store.topic_create(root, "t1", "x")
        with pytest.raises(StoreError) as exc:
            store.topic_create(root, "t1", "y")
        assert exc.value.code == "dir_exists"

    def test_idempotent_same_args_is_noop(self, root: Path):
        # Stage-2 idempotence: repeat `topic.create` with the exact same
        # `dir` + `description` is a no-op success — the existing TOPIC.md
        # is left untouched and no error is raised. The body on disk is
        # byte-identical and the size matches.
        store.topic_create(root, "t1", "same")
        first_size = (root / "t1" / "TOPIC.md").stat().st_size
        second = store.topic_create(root, "t1", "same")
        assert second.dir == "t1"
        assert second.description == "same"
        assert (root / "t1" / "TOPIC.md").read_text() == "same"
        assert (root / "t1" / "TOPIC.md").stat().st_size == first_size

    def test_idempotent_diff_description_still_dir_exists(self, root: Path):
        # Sanity: idempotence only kicks in for byte-identical args.
        store.topic_create(root, "t1", "same")
        with pytest.raises(StoreError) as exc:
            store.topic_create(root, "t1", "same ")  # trailing space differs
        assert exc.value.code == "dir_exists"

    def test_parent_missing_for_nested(self, root: Path):
        with pytest.raises(StoreError) as exc:
            store.topic_create(root, "a/b", "x")
        assert exc.value.code == "dir_missing"
        # nothing created
        assert not (root / "a").exists()

    def test_nested_after_parent(self, root: Path):
        store.topic_create(root, "a", "A")
        store.topic_create(root, "a/b", "B")
        assert (root / "a" / "TOPIC.md").is_file()
        assert (root / "a" / "b" / "TOPIC.md").is_file()

    def test_root_rejected(self, root: Path):
        with pytest.raises(StoreError) as exc:
            store.topic_create(root, "", "x")
        assert exc.value.code == "invalid_path"

    def test_foreign_existing_dir_rejected(self, root: Path):
        # caller hand-made an empty foreign dir on disk
        (root / "foreign").mkdir()
        with pytest.raises(StoreError) as exc:
            store.topic_create(root, "foreign", "x")
        assert exc.value.code == "dir_exists"


# --------------------------------------------------------------------------- #
# topic.read / topic.write                                                     #
# --------------------------------------------------------------------------- #


class TestTopicReadWrite:
    def test_read_existing(self, root: Path):
        store.topic_create(root, "t", "the desc")
        info = store.topic_read(root, "t")
        assert info is not None
        assert info.description == "the desc"

    def test_read_missing_returns_none(self, root: Path):
        assert store.topic_read(root, "nope") is None

    def test_read_missing_root(self, root: Path):
        assert store.topic_read(root, "") is None

    def test_write_overwrites(self, root: Path):
        store.topic_create(root, "t", "old")
        store.topic_write(root, "t", "new")
        assert (root / "t" / "TOPIC.md").read_text() == "new"

    def test_write_missing_dir(self, root: Path):
        with pytest.raises(StoreError) as exc:
            store.topic_write(root, "nope", "x")
        assert exc.value.code == "dir_missing"

    def test_write_root_rejected(self, root: Path):
        with pytest.raises(StoreError) as exc:
            store.topic_write(root, "", "x")
        assert exc.value.code == "invalid_path"


# --------------------------------------------------------------------------- #
# postit.create                                                               #
# --------------------------------------------------------------------------- #


class TestPostitCreate:
    def test_creates_file(self, root: Path):
        _topic(root, "t1")
        info = store.postit_create(root, "note", "body", dir="t1")
        assert (root / "t1" / "note.md").is_file()
        assert (root / "t1" / "note.md").read_text() == "body"
        assert info.body == "body"

    def test_empty_body_allowed(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "note", "", dir="t1")
        assert (root / "t1" / "note.md").read_text() == ""
        assert (root / "t1" / "note.md").stat().st_size == 0

    def test_at_root(self, root: Path):
        # root is exempt from topic-required check
        store.postit_create(root, "rootnote", "body", dir=".")
        assert (root / "rootnote.md").is_file()

    def test_case_insensitive_name_folds_to_lowercase(self, root: Path):
        # any case is folded on the way in; the on-disk filename is lowercase.
        store.postit_create(root, "MixedCase", "body", dir=".")
        assert (root / "mixedcase.md").is_file()
        assert not (root / "MixedCase.md").exists()

    def test_case_insensitive_round_trip(self, root: Path):
        # Create with one case, read/list/delete with another — all hit the
        # same on-disk file because every op lowercases the name first.
        store.postit_create(root, "Recall", "body", dir=".")
        info = store.postit_read(root, "RECALL", dir=".")
        assert info.body == "body"
        listing = store.postit_ls(root, dir=".")
        assert any(it.name == "recall" for it in listing)
        store.postit_delete(root, "recall", dir=".")
        with pytest.raises(StoreError) as exc:
            store.postit_read(root, "Recall", dir=".")
        assert exc.value.code == "not_found"

    def test_case_insensitive_already_exists(self, root: Path):
        store.postit_create(root, "Foo", "a", dir=".")
        # second create with different case must collide (same folded name).
        with pytest.raises(StoreError) as exc:
            store.postit_create(root, "foo", "b", dir=".")
        assert exc.value.code == "already_exists"

    def test_dir_missing(self, root: Path):
        with pytest.raises(StoreError) as exc:
            store.postit_create(root, "note", "body", dir="nope")
        assert exc.value.code == "dir_missing"

    def test_dir_no_topic_marker(self, root: Path):
        # foreign dir on disk without TOPIC.md
        (root / "foreign").mkdir()
        with pytest.raises(StoreError) as exc:
            store.postit_create(root, "note", "body", dir="foreign")
        assert exc.value.code == "dir_missing"

    def test_already_exists(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "note", "body", dir="t1")
        with pytest.raises(StoreError) as exc:
            store.postit_create(root, "note", "body", dir="t1")
        assert exc.value.code == "already_exists"

    def test_reserved_name_topic(self, root: Path):
        _topic(root, "t1")
        with pytest.raises(StoreError) as exc:
            store.postit_create(root, "TOPIC", "body", dir="t1")
        assert exc.value.code == "reserved_name"

    def test_invalid_name(self, root: Path):
        _topic(root, "t1")
        with pytest.raises(StoreError) as exc:
            store.postit_create(root, ".hidden", "body", dir="t1")
        assert exc.value.code == "invalid_name"

    def test_too_large(self, root: Path):
        _topic(root, "t1")
        big = "x" * (store.MAX_BODY_BYTES + 1)
        with pytest.raises(StoreError) as exc:
            store.postit_create(root, "note", big, dir="t1")
        assert exc.value.code == "too_large"


# --------------------------------------------------------------------------- #
# postit.append                                                              #
# --------------------------------------------------------------------------- #


class TestPostitAppend:
    def test_append_no_existing_newline(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "line1", dir="t1")  # no trailing \n
        info = store.postit_append(root, "n", "line2", dir="t1")
        assert info.body == "line1\nline2"

    def test_append_with_existing_newline(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "line1\n", dir="t1")
        info = store.postit_append(root, "n", "line2", dir="t1")
        assert info.body == "line1\nline2"

    def test_append_empty_existing(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "", dir="t1")
        info = store.postit_append(root, "n", "x", dir="t1")
        assert info.body == "x"

    def test_not_found(self, root: Path):
        _topic(root, "t1")
        with pytest.raises(StoreError) as exc:
            store.postit_append(root, "n", "x", dir="t1")
        assert exc.value.code == "not_found"

    def test_too_large_on_append(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "x", dir="t1")
        big = "x" * (store.MAX_BODY_BYTES + 1)
        with pytest.raises(StoreError) as exc:
            store.postit_append(root, "n", big, dir="t1")
        assert exc.value.code == "too_large"


# --------------------------------------------------------------------------- #
# postit.overwrite                                                           #
# --------------------------------------------------------------------------- #


class TestPostitOverwrite:
    def test_overwrite(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "old", dir="t1")
        info = store.postit_overwrite(root, "n", "new", dir="t1")
        assert info.body == "new"
        assert (root / "t1" / "n.md").read_text() == "new"

    def test_overwrite_empty(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "old", dir="t1")
        info = store.postit_overwrite(root, "n", "", dir="t1")
        assert info.body == ""
        assert (root / "t1" / "n.md").read_text() == ""

    def test_not_found(self, root: Path):
        _topic(root, "t1")
        with pytest.raises(StoreError) as exc:
            store.postit_overwrite(root, "n", "x", dir="t1")
        assert exc.value.code == "not_found"

    def test_too_large_on_overwrite(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "x", dir="t1")
        big = "x" * (store.MAX_BODY_BYTES + 1)
        with pytest.raises(StoreError) as exc:
            store.postit_overwrite(root, "n", big, dir="t1")
        assert exc.value.code == "too_large"

    def test_atomic_write_leaves_no_tmp(self, root: Path):
        """`_atomic_write_text` must clean up its tmp file on success."""
        _topic(root, "t1")
        store.postit_create(root, "n", "body", dir="t1")
        note = root / "t1" / "n.md"
        # No stray tmp files around the note after a write.
        store.postit_overwrite(root, "n", "body2", dir="t1")
        siblings = list(note.parent.iterdir())
        assert not any(name.endswith(".agentpostit.tmp") for name in
                       (p.name for p in siblings))


# --------------------------------------------------------------------------- #
# _atomic_write_text & fsync helpers                                         #
# --------------------------------------------------------------------------- #


class TestAtomicWriteHelpers:
    def test_atomic_write_cleans_tmp_on_failure(self, root: Path, monkeypatch):
        """On a raised error mid-write, tmp unlink still attempted."""
        _topic(root, "t1")
        store.postit_create(root, "n", "body", dir="t1")
        note = root / "t1" / "n.md"
        # Force os.replace to blow up so the except branch runs.
        real_replace = store.os.replace
        def boom(src, dst):
            raise OSError("simulated")
        monkeypatch.setattr(store.os, "replace", boom)
        with pytest.raises(OSError):
            store._atomic_write_text(note, "new body")
        # tmp should have been unlinked (best-effort) by the cleanup branch.
        siblings = list(note.parent.iterdir())
        assert not any(name.endswith(".agentpostit.tmp") for name in
                       (p.name for p in siblings))
        # Original body untouched.
        assert note.read_text() == "body"
        monkeypatch.setattr(store.os, "replace", real_replace)

    def test_fsync_helpers_do_not_raise_on_invalid_fd(self, tmp_path: Path):
        """`_fsync_fd` swallows OSError on closed/unsupported fds."""
        fd = os.open(str(tmp_path / "x"), os.O_CREAT | os.O_WRONLY)
        os.close(fd)
        # fd now closed → fsync raises OSError; helper must swallow.
        store._fsync_fd(fd)  # should not raise

    def test_fsync_dir_swallows_errors(self, tmp_path: Path):
        """`_fsync_dir` is best-effort and must not raise on normal dirs."""
        store._fsync_dir(tmp_path / "nonexistent")  # parent missing → noop
        store._fsync_dir(tmp_path)  # valid dir → fsync (no assertion)


# --------------------------------------------------------------------------- #
# postit.rename                                                               #
# --------------------------------------------------------------------------- #


class TestPostitRename:
    def test_basic(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "old", "body", dir="t1")
        info = store.postit_rename(root, "old", "new", dir="t1")
        assert info.name == "new"
        assert not (root / "t1" / "old.md").exists()
        assert (root / "t1" / "new.md").read_text() == "body"

    def test_same_name_no_op(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "body", dir="t1")
        with pytest.raises(StoreError) as exc:
            store.postit_rename(root, "n", "n", dir="t1")
        assert exc.value.code == "no_op"

    def test_to_existing(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "a", "A", dir="t1")
        store.postit_create(root, "b", "B", dir="t1")
        with pytest.raises(StoreError) as exc:
            store.postit_rename(root, "a", "b", dir="t1")
        assert exc.value.code == "already_exists"

    def test_to_reserved(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "body", dir="t1")
        with pytest.raises(StoreError) as exc:
            store.postit_rename(root, "n", "TOPIC", dir="t1")
        assert exc.value.code == "reserved_name"

    def test_source_missing(self, root: Path):
        _topic(root, "t1")
        with pytest.raises(StoreError) as exc:
            store.postit_rename(root, "ghost", "ghost2", dir="t1")
        assert exc.value.code == "not_found"


# --------------------------------------------------------------------------- #
# postit.delete                                                               #
# --------------------------------------------------------------------------- #


class TestPostitDelete:
    def test_deletes_file(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "body", dir="t1")
        store.postit_delete(root, "n", dir="t1")
        assert not (root / "t1" / "n.md").exists()

    def test_dir_survives(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "body", dir="t1")
        store.postit_delete(root, "n", dir="t1")
        # locked decision: dir + TOPIC.md survive empty
        assert (root / "t1").is_dir()
        assert (root / "t1" / "TOPIC.md").is_file()

    def test_case_insensitive_delete_round_trip(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "MixedCase", "body", dir="t1")
        assert (root / "t1" / "mixedcase.md").is_file()
        store.postit_delete(root, "MIXEDCASE", dir="t1")
        assert not (root / "t1" / "mixedcase.md").exists()

    def test_redelete_not_found(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "body", dir="t1")
        store.postit_delete(root, "n", dir="t1")
        with pytest.raises(StoreError) as exc:
            store.postit_delete(root, "n", dir="t1")
        assert exc.value.code == "not_found"


# --------------------------------------------------------------------------- #
# postit.read                                                                 #
# --------------------------------------------------------------------------- #


class TestPostitRead:
    def test_read(self, root: Path):
        store.postit_create(root, "rootnote", "body", dir=".")
        info = store.postit_read(root, "rootnote", dir=".")
        assert info.name == "rootnote"
        assert info.body == "body"
        assert info.dir == ""
        assert info.size == len("body".encode())

    def test_missing(self, root: Path):
        with pytest.raises(StoreError) as exc:
            store.postit_read(root, "ghost")
        assert exc.value.code == "not_found"


# --------------------------------------------------------------------------- #
# postit.read_section / read_lines  (deeper section parser test in sections)  #
# --------------------------------------------------------------------------- #


class TestPostitReadSection:
    def test_returns_slice(self, root: Path):
        _topic(root, "t1")
        body = "## Setup\ndo X\n### Sub\nfoo\n## Notes\nbar\n"
        store.postit_create(root, "n", body, dir="t1")
        out = store.postit_read_section(root, "n", "Setup", dir="t1")
        assert out.body == "## Setup\ndo X\n### Sub\nfoo\n"

    def test_no_match_none(self, root: Path):
        _topic(root, "t1")
        store.postit_create(root, "n", "## A\nx\n", dir="t1")
        out = store.postit_read_section(root, "n", "Missing", dir="t1")
        assert out.body is None


class TestPostitReadLines:
    def test_valid_range(self, root: Path):
        body = "l1\nl2\nl3\nl4\nl5\n"
        store.postit_create(root, "n", body)
        out = store.postit_read_lines(root, "n", 2, 4)
        assert out.start == 2
        assert out.end == 4
        assert out.total_lines == 5
        assert out.lines == "l2\nl3\nl4\n"

    def test_end_beyond_eof_clamps(self, root: Path):
        body = "a\nb\nc\n"
        store.postit_create(root, "n", body)
        out = store.postit_read_lines(root, "n", 1, 50)
        assert out.end == 3
        assert out.lines == "a\nb\nc\n"
        assert out.total_lines == 3

    def test_empty_file(self, root: Path):
        store.postit_create(root, "n", "")
        out = store.postit_read_lines(root, "n", 1, 5)
        assert out.lines == ""
        assert out.total_lines == 0

    def test_start_too_low(self, root: Path):
        store.postit_create(root, "n", "x\n")
        with pytest.raises(StoreError) as exc:
            store.postit_read_lines(root, "n", 0, 1)
        assert exc.value.code == "invalid_range"

    def test_end_lt_start(self, root: Path):
        store.postit_create(root, "n", "x\n")
        with pytest.raises(StoreError) as exc:
            store.postit_read_lines(root, "n", 3, 2)
        assert exc.value.code == "invalid_range"

    def test_no_trailing_newline_last_line(self, root: Path):
        store.postit_create(root, "n", "a\nb")
        out = store.postit_read_lines(root, "n", 2, 5)
        assert out.end == 2
        assert out.total_lines == 2
        assert out.lines == "b"


# --------------------------------------------------------------------------- #
# postit.ls                                                                   #
# --------------------------------------------------------------------------- #


class TestPostitLs:
    def test_dir_mode_flat(self, root: Path):
        store.postit_create(root, "rootnote", "x")
        _topic(root, "t1")
        store.postit_create(root, "tnote", "y", dir="t1")
        out = store.postit_ls(root)
        types = [
            (it.name, type(it).__name__) for it in out
        ]
        assert ("rootnote", "LsPostitItem") in types
        assert ("t1", "LsDirItem") in types
        # foreign files are ignored: drop a .txt
        (root / "stray.txt").write_text("ignore me")
        out2 = store.postit_ls(root)
        names = {it.name for it in out2}
        assert "stray.txt" not in names
        # TOPIC.md never listed
        assert "TOPIC" not in names

    def test_dir_mode_has_topic_field(self, root: Path):
        _topic(root, "t1")
        out = store.postit_ls(root)
        dir_item = next(it for it in out if it.name == "t1")
        assert dir_item.has_topic is True
        assert dir_item.topic_preview == "desc"

    def test_foreign_subdir_has_topic_false(self, root: Path):
        (root / "foreign").mkdir()
        out = store.postit_ls(root)
        dir_item = next(it for it in out if it.name == "foreign")
        assert dir_item.has_topic is False
        assert dir_item.topic_preview is None

    def test_recursive_walk(self, root: Path):
        _topic(root, "t")
        _topic(root, "t/sub")
        store.postit_create(root, "tnote", "x", dir="t")
        store.postit_create(root, "subnote", "y", dir="t/sub")
        store.postit_create(root, "rootnote", "z")
        out = store.postit_ls(root, recursive=True)
        names = {it.name for it in out}
        assert "t" in names            # dir
        assert "t/sub" in names        # nested dir
        assert "t/tnote" in names
        assert "t/sub/subnote" in names
        assert "rootnote" in names

    def test_recursive_sort_byte_order(self, root: Path):
        # remember-me/foo.md sorts before remember-me2.md (because '/' < '2')
        _topic(root, "remember-me")
        store.postit_create(root, "foo", "x", dir="remember-me")
        store.postit_create(root, "remember-me2", "z")
        out = store.postit_ls(root, recursive=True)
        names = [it.name for it in out if isinstance(it, store.LsPostitItem)]
        i1 = names.index("remember-me/foo")
        i2 = names.index("remember-me2")
        assert i1 < i2, (names, "expected '/foo' to sort before '2'")

    def test_note_mode(self, root: Path):
        _topic(root, "t1")
        body = "## A\nx\n## B\ny\n"
        store.postit_create(root, "n", body, dir="t1")
        out = store.postit_ls(root, dir="t1", name="n")
        assert isinstance(out, store.LsNoteModeResult)
        headings = [(h.level, h.heading, h.line_no) for h in out.headings]
        assert headings == [(2, "A", 1), (2, "B", 3)]
        assert out.total_lines == 4

    def test_note_mode_reserved(self, root: Path):
        _topic(root, "t1")
        with pytest.raises(StoreError) as exc:
            store.postit_ls(root, dir="t1", name="TOPIC")
        assert exc.value.code == "reserved_name"

    def test_note_mode_missing(self, root: Path):
        _topic(root, "t1")
        with pytest.raises(StoreError) as exc:
            store.postit_ls(root, dir="t1", name="ghost")
        assert exc.value.code == "not_found"