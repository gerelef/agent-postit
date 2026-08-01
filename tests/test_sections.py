from agent_postit.sections import parse_headings, read_section

SAMPLE = "## Setup\ndo X\n### Sub-step\nfoo\n## Notes\nbar\n"


def test_default_level_setup():
    out = read_section(SAMPLE, "Setup")
    assert out == "## Setup\ndo X\n### Sub-step\nfoo\n"


def test_case_insensitive_lower():
    assert read_section(SAMPLE, "setup") == "## Setup\ndo X\n### Sub-step\nfoo\n"


def test_case_insensitive_upper():
    assert read_section(SAMPLE, "SETUP") == "## Setup\ndo X\n### Sub-step\nfoo\n"


def test_substep_level3():
    assert read_section(SAMPLE, "Sub-step", level=3) == "### Sub-step\nfoo\n"


def test_setup_higher_level_no_match_returns_none():
    assert read_section(SAMPLE, "Setup", level=4) is None


def test_exact_not_substring():
    body = "## Auth\nfoo\n"
    assert read_section(body, "au") is None
    assert read_section(body, "Auth") == "## Auth\nfoo\n"


def test_no_match_returns_none():
    assert read_section("## A\nx\n", "Missing") is None


def test_close_form_stripped():
    body = "## foo ##\nbar\n"
    headings = parse_headings(body)
    assert headings[0].heading == "foo"
    assert read_section(body, "foo") == "## foo ##\nbar\n"


def test_setext_not_heading():
    body = "Foo\n===\nbody\n"
    headings = parse_headings(body)
    assert headings == []


def test_no_space_after_hash_not_heading():
    body = "##notahing\ntext\n"
    assert parse_headings(body) == []


def test_indented_code_block_not_heading():
    # 4 leading spaces -> indented code, not a heading
    body = "    ## Heading\ntext\n"
    assert parse_headings(body) == []


def test_three_leading_spaces_ok():
    body = "   ## Heading\ntext\n"
    hs = parse_headings(body)
    assert len(hs) == 1
    assert hs[0].level == 2
    assert hs[0].heading == "Heading"


def test_fence_excludes_heading():
    body = "```\n## Inside\n```\n## Outside\nx\n"
    hs = parse_headings(body)
    assert [h.heading for h in hs] == ["Outside"]


def test_tilde_fence_excludes_heading():
    body = "~~~\n## Inside\n~~~\n## Outside\nx\n"
    hs = parse_headings(body)
    assert [h.heading for h in hs] == ["Outside"]


def test_longer_close_fence_closes():
    # Open with ```` (4), close with ``` (3) -> NOT closed, still fenced.
    body = "````\n## Inside\n```\n## StillInside\n````\n## Outside\nx\n"
    hs = parse_headings(body)
    assert [h.heading for h in hs] == ["Outside"]


def test_section_includes_subheaders_through_eof():
    body = "## Setup\n### Sub\nbody\n"
    assert read_section(body, "Setup") == "## Setup\n### Sub\nbody\n"


def test_section_stops_at_higher_or_equal_level():
    # Sub-step (3) section stops at the next level<=3 heading (the `## Notes`
    # level-2 is <= 3, so it stops there).
    assert read_section(SAMPLE, "Sub-step", level=3) == "### Sub-step\nfoo\n"


def test_blank_line_before_heading_not_required():
    body = "## A\n## B\nx\n"
    assert read_section(body, "A") == "## A\n"
    assert read_section(body, "B") == "## B\nx\n"