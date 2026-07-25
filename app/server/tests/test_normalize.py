from filmpaw_server.normalize import bidirectional_match, normalize


def test_traditional_to_simplified() -> None:
    assert normalize("倉木華") == normalize("仓木华")


def test_nfkc_fullwidth_parens_and_latin() -> None:
    assert normalize("小红(仓木)") == normalize("小红(仓木)")
    assert normalize("ABC") == "abc"
    assert normalize("  x  ") == "x"


def test_bidirectional_forward() -> None:
    # record "小红(仓木)" contains query "小红"
    assert bidirectional_match(normalize("小红(仓木)"), normalize("小红"))


def test_bidirectional_reverse() -> None:
    # query "小红(仓木)" contains record "小红" (record >= 2 chars)
    assert bidirectional_match(normalize("小红"), normalize("小红(仓木)"))


def test_reverse_guard_single_char_record() -> None:
    assert not bidirectional_match(normalize("红"), normalize("小红(仓木)"))


def test_empty_query_no_match() -> None:
    assert not bidirectional_match("abc", "")
