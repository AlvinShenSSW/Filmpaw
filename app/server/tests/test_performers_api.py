"""Issue #3 acceptance tests: search, D5 aliases, pagination, thumb, open."""

from pathlib import Path

import pytest

import filmpaw_server.routes_performers as rp
from tests.conftest import add_performer_folder


@pytest.fixture()
def opened(monkeypatch) -> list[str]:
    """Capture explorer invocations instead of spawning windows."""
    calls: list[str] = []
    monkeypatch.setattr(rp, "open_in_explorer", calls.append)
    return calls


def _seed(client, source_dir: Path, names: list[str], jpg: set[str] | None = None) -> int:
    for n in names:
        add_performer_folder(source_dir, n, with_jpg=bool(jpg and n in jpg))
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")
    return sid


def _items(client, **params) -> dict:
    resp = client.get("/api/performers", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ search


def test_bidirectional_search_forward_and_reverse(client, source_dir) -> None:
    _seed(client, source_dir, ["小红", "小红(仓木)", "倉木華"])
    fwd = _items(client, q="小红")
    assert {i["name"] for i in fwd["items"]} == {"小红", "小红(仓木)"}
    rev = _items(client, q="小红(仓木)")
    assert {i["name"] for i in rev["items"]} == {"小红", "小红(仓木)"}
    zh = _items(client, q="仓木华")
    assert {i["name"] for i in zh["items"]} == {"倉木華"}


def test_search_via_alias_returns_whole_group(client, source_dir, tmp_path) -> None:
    _seed(client, source_dir, ["倉木華"])
    other = tmp_path / "nas2" / "女优V"
    other.mkdir(parents=True)
    add_performer_folder(other, "倉木華")
    sid2 = client.post("/api/sources", json={"unc_path": str(other)}).json()["id"]
    client.post(f"/api/sources/{sid2}/scan")

    a = _items(client, q="倉木華")["items"][0]
    client.post(f"/api/performers/{a['id']}/aliases", json={"alias": "华姐"})

    hit = _items(client, q="华姐")
    assert len(hit["items"]) == 2  # both same-name records, across sources
    assert all(any(al["alias"] == "华姐" for al in i["aliases"]) for i in hit["items"])


# --------------------------------------------------------------- D5 aliases


def test_alias_shared_projection_and_group_unique(client, source_dir, tmp_path) -> None:
    _seed(client, source_dir, ["倉木華"])
    other = tmp_path / "nas2" / "女优V"
    other.mkdir(parents=True)
    add_performer_folder(other, "倉木華")
    sid2 = client.post("/api/sources", json={"unc_path": str(other)}).json()["id"]
    client.post(f"/api/sources/{sid2}/scan")

    items = _items(client)["items"]
    a, b = items[0], items[1]
    r = client.post(f"/api/performers/{a['id']}/aliases", json={"alias": "华姐"})
    assert r.status_code == 201

    # projection: BOTH rows list the alias in the plain list view
    items = _items(client)["items"]
    assert all(any(al["alias"] == "华姐" for al in i["aliases"]) for i in items)

    # cross-add on the other record -> 409 (group-unique)
    assert (
        client.post(f"/api/performers/{b['id']}/aliases", json={"alias": "华姐"}).status_code
        == 409
    )
    # traditional/simplified variant of the same alias -> still 409 (norm)
    assert (
        client.post(f"/api/performers/{b['id']}/aliases", json={"alias": "華姐"}).status_code
        == 409
    )
    # alias equal to the group name -> 409
    assert (
        client.post(f"/api/performers/{b['id']}/aliases", json={"alias": "仓木华"}).status_code
        == 409
    )

    # delete via the projected chip on B -> gone everywhere
    alias_id = _items(client)["items"][0]["aliases"][0]["id"]
    assert client.delete(f"/api/aliases/{alias_id}").status_code == 204
    assert all(i["aliases"] == [] for i in _items(client)["items"])


def test_alias_lifecycle_survives_record_deletion(client, source_dir, tmp_path) -> None:
    """D5: deleting records/sources never touches group aliases; orphaned
    aliases are invisible and revive when the name reappears."""
    import shutil

    d = add_performer_folder(source_dir, "小小白")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")
    pid = _items(client)["items"][0]["id"]
    client.post(f"/api/performers/{pid}/aliases", json={"alias": "白白"})

    # remove folder -> missing -> delete the record
    shutil.rmtree(d)
    client.post(f"/api/sources/{sid}/scan")
    assert client.delete(f"/api/performers/{pid}").status_code == 204

    # orphan alias invisible (search joins performers)
    assert _items(client, q="白白")["items"] == []

    # name reappears -> alias revives
    add_performer_folder(source_dir, "小小白")
    client.post(f"/api/sources/{sid}/scan")
    hit = _items(client, q="白白")
    assert len(hit["items"]) == 1 and hit["items"][0]["name"] == "小小白"


# --------------------------------------------------------------- pagination


def test_pagination_contract(client, source_dir) -> None:
    _seed(client, source_dir, [f"演员{chr(65 + i)}" for i in range(7)])

    p1 = _items(client, page=1, page_size=3)
    assert (p1["total"], p1["page"], p1["page_size"], p1["source_count"]) == (7, 1, 3, 1)
    assert len(p1["items"]) == 3
    p3 = _items(client, page=3, page_size=3)
    assert len(p3["items"]) == 1

    # stable sort: name_norm ASC, id ASC — page concat == full ordered list
    all_ids = [i["id"] for i in _items(client, page_size=200)["items"]]
    paged = [
        i["id"]
        for p in (1, 2, 3)
        for i in _items(client, page=p, page_size=3)["items"]
    ]
    assert paged == all_ids

    # limits
    assert client.get("/api/performers", params={"page_size": 201}).status_code == 422
    assert client.get("/api/performers", params={"page": 0}).status_code == 422


def test_filters_and_stats_ignore_pagination(client, source_dir, tmp_path) -> None:
    import shutil

    gone = add_performer_folder(source_dir, "已删者")
    _seed(client, source_dir, ["甲", "乙"])
    shutil.rmtree(gone)
    sid = client.get("/api/sources").json()[0]["id"]
    client.post(f"/api/sources/{sid}/scan")

    both = _items(client, include_missing=True, page_size=1)
    assert both["total"] == 3  # total ignores pagination
    only = _items(client, include_missing=False)
    assert only["total"] == 2
    assert {i["name"] for i in only["items"]} == {"甲", "乙"}

    assert client.get("/api/performers", params={"source_id": 9999}).status_code == 422
    bysrc = _items(client, source_id=sid)
    assert bysrc["total"] == 3 and bysrc["source_count"] == 1


# -------------------------------------------------------------------- thumb


def test_thumb_endpoint(client, source_dir) -> None:
    _seed(client, source_dir, ["有图", "无图"], jpg={"有图"})
    items = {i["name"]: i for i in _items(client)["items"]}
    assert items["有图"]["has_thumb"] is True
    assert items["无图"]["has_thumb"] is False

    ok = client.get(f"/api/performers/{items['有图']['id']}/thumb")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/jpeg"
    assert "max-age" in ok.headers["cache-control"]
    assert client.get(f"/api/performers/{items['无图']['id']}/thumb").status_code == 404


# ------------------------------------------------------------ open / pair


def test_open_marks_missing_on_gone_path(client, source_dir, opened) -> None:
    import shutil

    d = add_performer_folder(source_dir, "开我")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")
    pid = _items(client)["items"][0]["id"]

    assert client.post(f"/api/performers/{pid}/open").status_code == 204
    assert len(opened) == 1

    shutil.rmtree(d)
    assert client.post(f"/api/performers/{pid}/open").status_code == 404
    assert _items(client)["items"][0]["is_missing"] is True  # flagged by open


def test_open_pair_semantics(client, source_dir, tmp_path, opened) -> None:
    add_performer_folder(source_dir, "配对")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")
    pid = _items(client)["items"][0]["id"]
    local = tmp_path / "downloads" / "配对"
    local.mkdir(parents=True)

    ok = client.post("/api/open-pair", json={"local_path": str(local), "performer_id": pid})
    assert ok.status_code == 204
    assert len(opened) == 2  # both folders

    # 422 local gone
    bad = client.post(
        "/api/open-pair", json={"local_path": str(tmp_path / "nope"), "performer_id": pid}
    )
    assert bad.status_code == 422
    # 404 performer unknown
    assert (
        client.post("/api/open-pair", json={"local_path": str(local), "performer_id": "x"})
        .status_code
        == 404
    )
    # 409 performer missing
    import shutil

    shutil.rmtree(source_dir / "配对")
    client.post(f"/api/sources/{sid}/scan")
    assert (
        client.post("/api/open-pair", json={"local_path": str(local), "performer_id": pid})
        .status_code
        == 409
    )


def test_local_subdirs_and_settings(client, tmp_path) -> None:
    root = tmp_path / "dl"
    (root / "甲").mkdir(parents=True)
    (root / "乙").mkdir()
    r = client.get("/api/local/subdirs", params={"path": str(root)})
    assert r.status_code == 200 and r.json()["subdirs"] == ["乙", "甲"] or r.json()[
        "subdirs"
    ] == sorted(["甲", "乙"])
    assert client.get("/api/local/subdirs", params={"path": str(root / "no")}).status_code == 422

    first = client.get("/api/settings").json()
    assert first["last_local_dir"] is None
    assert first["db_path"]  # settings carries the db path for the UI footer
    client.put("/api/settings", json={"last_local_dir": str(root)})
    assert client.get("/api/settings").json()["last_local_dir"] == str(root)


# ------------------------------------------------------------ delete/purge


def test_delete_and_purge_missing(client, source_dir) -> None:
    import shutil

    d1 = add_performer_folder(source_dir, "删一")
    d2 = add_performer_folder(source_dir, "删二")
    add_performer_folder(source_dir, "留下")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")

    shutil.rmtree(d1)
    shutil.rmtree(d2)
    client.post(f"/api/sources/{sid}/scan")

    items = {i["name"]: i for i in _items(client)["items"]}
    assert client.delete(f"/api/performers/{items['删一']['id']}").status_code == 204
    assert client.delete(f"/api/performers/{items['删一']['id']}").status_code == 404

    purged = client.post("/api/performers/purge-missing").json()
    assert purged == {"deleted": 1}  # 删二 only; 留下 untouched
    assert {i["name"] for i in _items(client)["items"]} == {"留下"}


def test_search_with_like_metacharacters(client, source_dir) -> None:
    """CTO regression: % and _ in names must work in both directions
    (LIKE escaping must not pollute the INSTR reverse-containment param)."""
    _seed(client, source_dir, ["100%女神"])
    assert _items(client, q="100%")["items"][0]["name"] == "100%女神"
    hit = _items(client, q="100%女神(新)")  # reverse: query contains record
    assert hit["items"][0]["name"] == "100%女神"


def test_404_paths_leave_no_open_transaction(client, source_dir) -> None:
    """CTO regression: 0-row DELETEs must roll back their implicit txn."""
    client.delete("/api/performers/nope")
    client.delete("/api/aliases/99999")
    client.delete("/api/sources/99999")
    # if a dirty txn leaked, the next write on the shared conn would behave
    # oddly; assert a clean write cycle works and reads see consistent state
    _seed(client, source_dir, ["正常"])
    assert _items(client)["total"] == 1


def test_single_char_alias_no_reverse_noise(client, source_dir) -> None:
    """Codex outer-gate finding (refuted, locked in as regression): the
    >=2-char reverse guard applies to aliases identically to names — a
    1-char alias must not reverse-match longer queries containing it."""
    _seed(client, source_dir, ["小白花"])
    pid = _items(client)["items"][0]["id"]
    assert client.post(f"/api/performers/{pid}/aliases", json={"alias": "白"}).status_code == 201
    assert _items(client, q="大白鲨(新)")["items"] == []   # no reverse noise
    assert _items(client, q="白")["items"][0]["name"] == "小白花"  # forward OK


def test_open_clears_missing_when_folder_is_back(client, source_dir, opened) -> None:
    """Kimi P2: opening a missing row whose folder is reachable again
    clears the flag (it was just offline at last scan)."""
    import shutil

    d = add_performer_folder(source_dir, "回来了")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")
    pid = _items(client)["items"][0]["id"]

    shutil.rmtree(d)
    client.post(f"/api/sources/{sid}/scan")
    assert _items(client)["items"][0]["is_missing"] is True

    add_performer_folder(source_dir, "回来了")  # folder reappears (no rescan)
    assert client.post(f"/api/performers/{pid}/open").status_code == 204
    assert _items(client)["items"][0]["is_missing"] is False  # un-flagged
    assert len(opened) == 1
