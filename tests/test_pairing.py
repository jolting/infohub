from infohub.pairing import ISSUE, OK, PENDING, DisplayAuth


def make(data=None):
    saved = []
    return DisplayAuth(data, saved.append), saved


def test_unknown_display_waits():
    auth, _ = make()
    assert auth.check("r1", None) == PENDING
    assert auth.check("r1", "anything") == PENDING
    assert auth.check(None, None) == PENDING


def test_approve_then_issue_then_ok():
    auth, saved = make()
    auth.approve("r1")
    assert auth.check("r1", None) == ISSUE
    token = auth.issue("r1")
    assert auth.check("r1", token) == OK
    assert auth.check("r1", None) == PENDING
    assert auth.check("r1", token + "x") == PENDING
    assert saved, "changes are saved"


def test_only_a_hash_is_stored():
    auth, saved = make()
    auth.approve("r1")
    token = auth.issue("r1")
    assert token not in repr(saved[-1])
    assert token not in repr(auth.as_data())


def test_stored_data_survives_a_restart():
    auth, saved = make()
    auth.approve("r1")
    token = auth.issue("r1")
    again, _ = make(saved[-1])
    assert again.check("r1", token) == OK


def test_approving_again_replaces_the_token():
    auth, _ = make()
    auth.approve("r1")
    old = auth.issue("r1")
    auth.approve("r1")
    assert auth.check("r1", old) == ISSUE
    new = auth.issue("r1")
    assert auth.check("r1", old) == PENDING
    assert auth.check("r1", new) == OK


def test_forget():
    auth, _ = make()
    auth.approve("r1")
    token = auth.issue("r1")
    auth.forget("r1")
    assert auth.check("r1", token) == PENDING
    assert not auth.is_approved("r1")


def test_grandfather_once():
    auth, saved = make()
    auth.grandfather(["r1", "kitchen"])
    assert auth.check("r1", None) == ISSUE
    assert auth.check("kitchen", None) == ISSUE
    # a later start doesn't approve displays seen since
    again, _ = make(saved[-1])
    again.grandfather(["intruder"])
    assert again.check("intruder", None) == PENDING


def test_pending_listed_with_reason():
    auth, _ = make()
    auth.note_pending("new-one", "192.168.1.50")
    auth.approve("r1")
    auth.issue("r1")
    auth.note_pending("r1", "192.168.1.51")  # a wrong token for a paired display
    rows = {(d["device_id"], d["approved"]): d for d in auth.displays()}
    assert rows[("new-one", False)]["reason"] == "new"
    assert rows[("new-one", False)]["address"] == "192.168.1.50"
    assert rows[("r1", True)]["paired"] is True
    assert rows[("r1", False)]["reason"] == "token"


def test_issue_clears_pending():
    auth, _ = make()
    auth.note_pending("r1", None)
    auth.approve("r1")
    auth.issue("r1")
    assert "r1" not in auth.pending
