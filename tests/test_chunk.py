from hermes_xmpp.chunk import split_message


def test_short_message_is_untouched():
    assert split_message("hello") == ["hello"]


def test_empty_message_is_dropped():
    assert split_message("") == []
    assert split_message("   \n\n ") == []


def test_every_chunk_fits_the_limit():
    text = "\n\n".join(f"paragraph {i} " + "word " * 60 for i in range(40))
    chunks = split_message(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "paragraph 39" in chunks[-1]


def test_split_prefers_line_boundaries():
    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(60))
    chunks = split_message(text, limit=400)
    assert all(len(c) <= 400 for c in chunks)
    for chunk in chunks:
        for line in chunk.split("\n"):
            assert line.startswith("line ") or line.startswith("x")


def test_code_fence_is_reopened_and_closed_around_splits():
    body = "\n".join(f"print({i})" for i in range(200))
    text = f"before\n```python\n{body}\n```\nafter"
    chunks = split_message(text, limit=400)
    assert len(chunks) > 1
    assert all(len(c) <= 400 for c in chunks)
    for chunk in chunks:
        fences = [line for line in chunk.split("\n") if line.strip().startswith("```")]
        assert len(fences) % 2 == 0, f"unbalanced fence in chunk:\n{chunk}"
    assert chunks[0].startswith("before")
    assert chunks[-1].rstrip().endswith("after")


def test_unbroken_line_is_hard_split():
    chunks = split_message("x" * 1000, limit=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks) == "x" * 1000
