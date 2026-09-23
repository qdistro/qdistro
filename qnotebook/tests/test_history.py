from qnotebook.history import NavigationHistory


def test_initial_empty():
    h = NavigationHistory()
    assert h.current is None
    assert not h.can_go_back()
    assert not h.can_go_forward()


def test_push_sets_current():
    h = NavigationHistory()
    h.push("A")
    assert h.current == "A"


def test_push_same_page_noop():
    h = NavigationHistory()
    h.push("A")
    h.push("A")
    assert not h.can_go_back()


def test_back_forward():
    h = NavigationHistory()
    h.push("A")
    h.push("B")
    h.push("C")
    assert h.can_go_back()
    assert h.go_back() == "B"
    assert h.go_back() == "A"
    assert not h.can_go_back()
    assert h.go_forward() == "B"
    assert h.go_forward() == "C"
    assert not h.can_go_forward()


def test_push_clears_forward():
    h = NavigationHistory()
    h.push("A")
    h.push("B")
    h.go_back()
    assert h.can_go_forward()
    h.push("C")
    assert not h.can_go_forward()


def test_limit():
    h = NavigationHistory(limit=2)
    h.push("A")
    h.push("B")
    h.push("C")
    h.push("D")
    # Only last 2 in back stack
    assert h.go_back() == "C"
    assert h.go_back() == "B"
    assert not h.can_go_back()
