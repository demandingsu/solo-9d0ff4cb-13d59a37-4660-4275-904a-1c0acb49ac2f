"""更正后批量派卷：回避原评分教师、名额限制、整批发号/不发号。"""

from helpers import claim_first, paper, setup_layout


def dispatch(client, papers, teachers, *, expected=200):
    r = client.post("/dispatches", json={"papers": papers, "teachers": teachers})
    assert r.status_code == expected, r.text
    body = r.json()
    if expected != 200:
        # 退回时业务原因在 detail.details 里；返回整批结果便于断言
        assert body["detail"]["code"] == "DISPATCH_REJECTED", body
        return body["detail"]["details"]
    return body


def paper_ref(client, paper_no: str) -> dict:
    p = paper(client, paper_no)
    return {"paper_no": paper_no, "answer_id": p["answer_id"]}


def split_scored_paper(client):
    """P1 三块由 T-OLD 评分后拆分为 P1[B1,B2] + P2[B3]，返回各卷引用与旧领题号。"""
    setup_layout(client, {"P1": ["B1", "B2", "B3"]})
    old = claim_first(client, "T-OLD")
    old_no = old["claim_no"]
    assert client.post(f"/claims/{old_no}/score", json={"score": 80}).status_code == 200
    r = client.post(
        "/corrections",
        json={
            "assignments": [
                {"paper_no": "P1", "blocks": ["B1", "B2"]},
                {"paper_no": "P2", "blocks": ["B3"]},
            ]
        },
    )
    assert r.status_code == 200, r.text
    return old_no


# --------------------------------------------------------------------------- #
# 正常派发
# --------------------------------------------------------------------------- #

def test_dispatch_assigns_split_papers_to_new_teacher_and_scores_work(client):
    split_scored_paper(client)
    refs = [paper_ref(client, "P1"), paper_ref(client, "P2")]

    body = dispatch(
        client, refs, [{"teacher_id": "T-NEW", "quota": 2}]
    )
    assert body["status"] == "issued"
    assert body["paper_count"] == 2
    assert body["recused_teachers"] == ["T-OLD"]

    by_paper = {p["paper_no"]: p for p in body["papers"]}
    assert set(by_paper) == {"P1", "P2"}
    for item in by_paper.values():
        assert item["teacher_id"] == "T-NEW"
        assert isinstance(item["claim_no"], int)
        # 派号响应给教务看卷号；教师用领题号领取，领取内容不含卷号
        detail = client.get(f"/claims/{item['claim_no']}")
        assert detail.status_code == 200
        assert detail.json()["teacher_id"] == "T-NEW"
        assert "paper_no" not in detail.json()

    # 两卷都已“已领取待交分”
    for pno in ("P1", "P2"):
        p = paper(client, pno)
        assert p["status"] == "pending" and p["grading_status"] == "claimed"
        assert p["teacher_id"] == "T-NEW"

    # 教师照常凭号交分
    c1 = by_paper["P1"]["claim_no"]
    c2 = by_paper["P2"]["claim_no"]
    assert client.post(f"/claims/{c1}/score", json={"score": 71}).status_code == 200
    assert client.post(f"/claims/{c2}/score", json={"score": 72}).status_code == 200
    assert paper(client, "P1")["score"] == 71
    assert paper(client, "P2")["score"] == 72


def test_dispatch_is_per_paper_and_quota_respected(client):
    split_scored_paper(client)
    refs = [paper_ref(client, "P1"), paper_ref(client, "P2")]

    body = dispatch(
        client,
        refs,
        [
            {"teacher_id": "T-A", "quota": 1},
            {"teacher_id": "T-B", "quota": 1},
            {"teacher_id": "T-OLD", "quota": 5},
        ],
    )
    assert body["status"] == "issued"
    counts = {t["teacher_id"]: t["assigned"] for t in body["teachers"]}
    assert counts["T-A"] == 1 and counts["T-B"] == 1
    assert counts["T-OLD"] == 0  # 原评分教师即使有名额也被回避
    for item in body["papers"]:
        assert item["teacher_id"] != "T-OLD"
    assert len({p["claim_no"] for p in body["papers"]}) == 2


def test_voided_old_score_still_recuses_and_multi_split_history_counts(client):
    """旧分已作废、答卷被拆/拼多次，原评分教师仍必须回避。"""
    setup_layout(client, {"P1": ["B1", "B2"]})
    old_no = claim_first(client, "T-OLD")["claim_no"]
    client.post(f"/claims/{old_no}/score", json={"score": 60})

    # 拆开再拼回再拆开：旧分早已 void，答案身份多次更换
    for batch in (
        [
            {"paper_no": "P1", "blocks": ["B1"]},
            {"paper_no": "P2", "blocks": ["B2"]},
        ],
        [{"paper_no": "P1", "blocks": ["B1", "B2"]}],
        [
            {"paper_no": "P1", "blocks": ["B1"]},
            {"paper_no": "P2", "blocks": ["B2"]},
        ],
    ):
        assert client.post("/corrections", json={"assignments": batch}).status_code == 200

    refs = [paper_ref(client, "P1"), paper_ref(client, "P2")]
    rejected = dispatch(
        client, refs, [{"teacher_id": "T-OLD", "quota": 2}], expected=409
    )
    assert rejected["status"] == "rejected"
    assert rejected["reasons"][0]["code"] == "DISPATCH_UNFILLABLE"
    assert "T-OLD" in rejected["reasons"][0]["recused_teachers"]

    ok = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 2}])
    assert ok["status"] == "issued"
    assert ok["recused_teachers"] == ["T-OLD"]


# --------------------------------------------------------------------------- #
# 退回整批的情形
# --------------------------------------------------------------------------- #

def test_answer_already_claimed_rejects_whole_batch_without_issuing(client):
    split_scored_paper(client)
    # P2 先被普通教师按答案编号领走（领取响应仍不含卷号）
    p2_ref = paper_ref(client, "P2")
    taken = client.post("/claims", json={"teacher_id": "T-OTHER", "answer_id": p2_ref["answer_id"]})
    assert taken.status_code == 200, taken.text
    assert [b["block_no"] for b in taken.json()["blocks"]] == ["B3"]

    refs = [paper_ref(client, "P1"), paper_ref(client, "P2")]
    body = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 2}], expected=409)
    codes = {r["code"] for r in body["reasons"]}
    assert "ANSWER_ALREADY_CLAIMED" in codes

    # 整批不发号：P1 仍是待领取（无人领取），T-NEW 没有拿到任何领题号
    p1 = paper(client, "P1")
    assert p1["status"] == "pending" and p1["grading_status"] == "unclaimed"
    assert p1["claim_no"] is None
    active = client.get("/claims?status=active").json()["claims"]
    assert [c["teacher_id"] for c in active] == ["T-OTHER"]  # 只有先前被领走的那一个


def test_changed_answer_rejected(client):
    split_scored_paper(client)
    stale = {"paper_no": "P1", "answer_id": "A-does-not-match"}
    refs = [stale, paper_ref(client, "P2")]
    body = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 2}], expected=409)
    assert any(r["code"] == "ANSWER_CHANGED" for r in body["reasons"])
    assert body["papers"] == []
    assert paper(client, "P1")["grading_status"] == "unclaimed"


def test_unfillable_due_to_quota_issues_nothing(client):
    split_scored_paper(client)
    refs = [paper_ref(client, "P1"), paper_ref(client, "P2")]
    body = dispatch(
        client,
        refs,
        [{"teacher_id": "T-A", "quota": 1}, {"teacher_id": "T-B", "quota": 0}],
        expected=409,
    )
    assert body["reasons"][0]["code"] == "DISPATCH_UNFILLABLE"
    assert body["reasons"][0]["unassigned_papers"] == ["P2"]  # P1 有 1 个名额可派
    for pno in ("P1", "P2"):
        assert paper(client, pno)["grading_status"] == "unclaimed"


def test_missing_teacher_on_relevant_old_score_rejects_batch(client):
    # 旧分是由没有 teacher_id 的盲领提交的
    setup_layout(client, {"P1": ["B1", "B2"]})
    blind = client.post("/claims", json={}).json()
    assert client.post(f"/claims/{blind['claim_no']}/score", json={"score": 50}).status_code == 200
    assert client.post(
        "/corrections",
        json={
            "assignments": [
                {"paper_no": "P1", "blocks": ["B1"]},
                {"paper_no": "P2", "blocks": ["B2"]},
            ]
        },
    ).status_code == 200

    refs = [paper_ref(client, "P1"), paper_ref(client, "P2")]
    body = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 2}], expected=409)
    codes = {r["code"] for r in body["reasons"]}
    assert "MISSING_TEACHER_ON_OLD_SCORE" in codes
    assert all(paper(client, p)["grading_status"] == "unclaimed" for p in ("P1", "P2"))


def test_batch_without_any_scored_history_rejected(client):
    setup_layout(client, {"P1": ["B1", "B2"]})
    # 从来没有交过分：不属于“更正后重评”批次
    refs = [paper_ref(client, "P1")]
    body = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 1}], expected=409)
    assert any(r["code"] == "NO_SCORED_HISTORY" for r in body["reasons"])
    assert paper(client, "P1")["grading_status"] == "unclaimed"


def test_paper_without_scored_blocks_in_batch_rejected(client):
    """每卷都要求至少一块曾出现在已评分答案里；纯新卷不能混进重评批次。"""
    setup_layout(client, {"P1": ["B1", "B2"]})
    client.post("/blocks", json={"blocks": ["B9"]})
    cno = claim_first(client, "T-OLD")["claim_no"]
    client.post(f"/claims/{cno}/score", json={"score": 88})
    # 拆分 P1，并把从未评分的 B9 一起归到 P3
    client.post(
        "/corrections",
        json={
            "assignments": [
                {"paper_no": "P1", "blocks": ["B1"]},
                {"paper_no": "PX", "blocks": ["B2"]},
                {"paper_no": "P3", "blocks": ["B9"]},
            ]
        },
    )
    refs = [paper_ref(client, "P1"), paper_ref(client, "P3")]
    body = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 2}], expected=409)
    assert any(
        r["code"] == "NO_SCORED_HISTORY" and r["paper_no"] == "P3"
        for r in body["reasons"]
    )
    assert all(paper(client, p)["grading_status"] != "claimed" for p in ("P1", "P3"))


def test_dispatch_input_validation(client):
    setup_layout(client, {"P1": ["B1"]})
    ref = paper_ref(client, "P1")

    for payload in (
        {"papers": [], "teachers": [{"teacher_id": "T", "quota": 1}]},
        {"papers": [ref], "teachers": []},
        {"papers": [ref], "teachers": [{"teacher_id": "T", "quota": -1}]},
        {"papers": [ref, ref], "teachers": [{"teacher_id": "T", "quota": 1}]},
        {"papers": [{"paper_no": "P1"}], "teachers": [{"teacher_id": "T", "quota": 1}]},
    ):
        r = client.post("/dispatches", json=payload)
        assert r.status_code == 422, (payload, r.text)
        assert r.json()["detail"]["code"] == "DISPATCH_INVALID"


def test_unknown_paper_and_empty_paper_rejected(client):
    split_scored_paper(client)
    refs = [paper_ref(client, "P1"), {"paper_no": "NOPE", "answer_id": "A-x"}]
    body = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 5}], expected=409)
    assert any(
        r["code"] == "PAPER_NOT_FOUND" and r["paper_no"] == "NOPE"
        for r in body["reasons"]
    )
    assert paper(client, "P1")["grading_status"] == "unclaimed"


def test_already_scored_ok_paper_rejected(client):
    # 一张卷已评分、另一张待评：整批退回，因为只派待评答案
    setup_layout(client, {"P1": ["B1", "B2"]})
    cno = claim_first(client, "T-OLD")["claim_no"]
    client.post(f"/claims/{cno}/score", json={"score": 90})
    # 造一张新的待评卷，同时 B1/B2 保持原样（已评分那卷不参与）
    client.post("/blocks", json={"blocks": ["B7"]})
    client.post(
        "/corrections",
        json={
            "assignments": [
                {"paper_no": "P1", "blocks": ["B1", "B2"]},  # intact，仍已评分
                {"paper_no": "P2", "blocks": ["B7"]},  # 新块
            ]
        },
    )
    refs = [paper_ref(client, "P1"), paper_ref(client, "P2")]
    body = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 2}], expected=409)
    codes = {r["code"] for r in body["reasons"]}
    assert "ANSWER_ALREADY_SCORED" in codes


def test_dispatch_records_are_queryable(client):
    split_scored_paper(client)
    refs = [paper_ref(client, "P1"), paper_ref(client, "P2")]
    ok = dispatch(client, refs, [{"teacher_id": "T-NEW", "quota": 2}])
    one = client.get(f"/dispatches/{ok['dispatch_id']}").json()
    assert one["status"] == "issued"
    assert one["result"]["paper_count"] == 2
    assert client.get("/dispatches").json()["dispatches"][0]["dispatch_id"] == ok["dispatch_id"]
    assert client.get("/dispatches/9999").status_code == 404
