import pytest

from agent_postit.paths import (
    InvalidNameError,
    InvalidPathError,
    normalize_dir,
    validate_name,
)


class TestNormalizeRoot:
    def test_empty(self):
        assert normalize_dir("") == ""

    def test_dot(self):
        assert normalize_dir(".") == ""

    def test_slash(self):
        assert normalize_dir("/") == ""

    def test_dot_slash(self):
        assert normalize_dir("./") == ""

    def test_dotdotd_slash(self):
        assert normalize_dir("/.") == ""


class TestNormalizeStrip:
    def test_trailing_slash(self):
        assert normalize_dir("foo/") == "foo"

    def test_leading_slash(self):
        assert normalize_dir("/foo") == "foo"

    def test_nested(self):
        assert normalize_dir("/a/b/c/") == "a/b/c"

    def test_double_slash_collapses(self):
        assert normalize_dir("a//b") == "a/b"

    def test_dot_component_skipped(self):
        assert normalize_dir("a/./b") == "a/b"


class TestNormalizeLowercases:
    def test_single_component(self):
        assert normalize_dir("Foo") == "foo"

    def test_nested_components(self):
        assert normalize_dir("/Projects/Remember-Me/") == "projects/remember-me"

    def test_mixed_case_preserved_in_original_not_normalized(self):
        # We never preserve case; callers may pass any case and we fold it.
        assert normalize_dir("AbC/DeF") == "abc/def"


class TestNormalizeRejects:
    def test_dotdot(self):
        with pytest.raises(InvalidPathError):
            normalize_dir("..")

    def test_dotdot_inside(self):
        with pytest.raises(InvalidPathError):
            normalize_dir("a/../b")

    def test_dotdot_trailing(self):
        with pytest.raises(InvalidPathError):
            normalize_dir("a/..")

    def test_nul(self):
        with pytest.raises(InvalidPathError):
            normalize_dir("a\0b")

    def test_absolute_after_strip_still_dotdot(self):
        with pytest.raises(InvalidPathError):
            normalize_dir("/../etc/passwd")


class TestValidateNameAccept:
    def test_plain(self):
        assert validate_name("foo") == "foo"

    def test_with_spaces(self):
        assert validate_name("Foo Bar") == "foo bar"

    def test_with_dot_inside(self):
        assert validate_name("a.b") == "a.b"

    def test_case_folding(self):
        assert validate_name("FooBar") == "foobar"

    def test_topic_lowercase_ok(self):
        # `topic` (any case) is reserved — folded to `topic` which equals the
        # reserved basename lowercase, so this now raises.
        with pytest.raises(InvalidNameError):
            validate_name("topic")


class TestValidateNameReject:
    def test_empty(self):
        with pytest.raises(InvalidNameError):
            validate_name("")

    def test_topic(self):
        with pytest.raises(InvalidNameError):
            validate_name("TOPIC")

    def test_topic_mixed_case_reserved(self):
        # any case variant of `topic` is reserved
        for variant in ("Topic", "tOpIc", "TOPIC", "topic"):
            with pytest.raises(InvalidNameError):
                validate_name(variant)

    def test_slash(self):
        with pytest.raises(InvalidNameError):
            validate_name("a/b")

    def test_nul(self):
        with pytest.raises(InvalidNameError):
            validate_name("a\0b")

    def test_newline(self):
        with pytest.raises(InvalidNameError):
            validate_name("a\nb")

    def test_cr(self):
        with pytest.raises(InvalidNameError):
            validate_name("a\rb")

    def test_leading_dot(self):
        with pytest.raises(InvalidNameError):
            validate_name(".hidden")

    def test_just_dot(self):
        with pytest.raises(InvalidNameError):
            validate_name(".")