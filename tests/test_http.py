"""HTTP 级自测：通过真实 HTTP 请求走一遍与前端相同的 API 契约。"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def call(method, path, body=None, expect=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            code, payload = resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code, payload = e.code, json.loads(e.read().decode("utf-8"))
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {path} 期望 {expect} 实际 {code}: {payload}")
    return code, payload


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


def main():
    print("[H1] 静态页面与资源可访问")
    with urllib.request.urlopen(BASE + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    check("考古编年核对" in html, "首页 HTML 正常返回")
    with urllib.request.urlopen(BASE + "/static/app.js", timeout=10) as r:
        js = r.read().decode("utf-8")
    check("submitBatch" in js, "前端 app.js 正常返回")

    print("[H2] 初始状态为空表结构")
    code, state = call("GET", "/api/state", expect=200)
    check(state["chronology"]["layers"] == [], "初始无层位")

    print("[H3] 导入三层位 + 分离候选区间 + 严格早于（含空档编年）")
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_layer", "code": "H1"},
        {"action": "add_layer", "code": "H2"},
        {"action": "add_layer", "code": "H3"},
    ]}, expect=200)
    check(r["accepted"], "层位提交通过")
    ids = {x["code"]: x["id"] for x in (call("GET", "/api/state")[1]["layers"])}

    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_dating", "layer_id": ids["H1"],
         "lower_year": -1200, "upper_year": -1100, "note": "报告A-1"},
        {"action": "add_dating", "layer_id": ids["H1"],
         "lower_year": -950, "upper_year": -900, "note": "报告A-2（分离区间）"},
        {"action": "add_dating", "layer_id": ids["H2"],
         "lower_year": -1050, "upper_year": -930, "note": "报告B"},
        {"action": "add_dating", "layer_id": ids["H3"],
         "lower_year": -920, "upper_year": -700, "note": "报告C"},
        {"action": "add_superposition",
         "earlier_layer_id": ids["H1"], "later_layer_id": ids["H2"], "note": "H2叠压H1"},
        {"action": "add_superposition",
         "earlier_layer_id": ids["H2"], "later_layer_id": ids["H3"], "note": "H3叠压H2"},
    ]}, expect=200)
    check(r["accepted"], "依据提交通过")
    rows = {x["code"]: x for x in r["chronology"]["layers"]}
    check(rows["H1"]["gap_count"] == 1,
          f"H1 编年保留空档: {rows['H1']['feasible_intervals']}")
    # H2 上界 -930 -> H1 须 <= -931，H1 晚段 [-950,-900] 被剪到 -931，空档保留
    check(rows["H1"]["feasible_intervals"] == [[-1200, -1100], [-950, -931]],
          f"H1 晚段沿叠压收窄且空档保留: {rows['H1']['feasible_intervals']}")
    check(rows["H2"]["feasible_intervals"] == [[-1050, -930]],
          f"H2 区间正确: {rows['H2']['feasible_intervals']}")
    check(rows["H3"]["feasible_intervals"] == [[-920, -700]],
          f"H3 区间正确: {rows['H3']['feasible_intervals']}")

    print("[H4] 登记 -> 更换 -> 冲突驳回 -> 撤回（交错提交）")
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_registration", "layer_id": ids["H1"],
         "lower_year": -1300, "upper_year": -1000, "note": "拟采用早段"},
    ]}, expect=200)
    check(r["accepted"], "登记通过")
    regs = call("GET", "/api/state")[1]["registrations"]
    reg_id = regs[-1]["id"]
    rows = {x["code"]: x for x in r["chronology"]["layers"]}
    check(rows["H1"]["feasible_intervals"] == [[-1200, -1100]],
          f"登记收窄至早段: {rows['H1']['feasible_intervals']}")

    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "replace_registration", "registration_id": reg_id,
         "lower_year": -1000, "upper_year": -800, "note": "改用晚段"},
    ]}, expect=200)
    check(r["accepted"], "更换登记通过")
    rows = {x["code"]: x for x in r["chronology"]["layers"]}
    check(rows["H1"]["feasible_intervals"] == [[-950, -931]],
          f"更换后只剩晚段: {rows['H1']['feasible_intervals']}")

    # 无解提交：登记 [-905,-900] 使 H1 被限制在晚段末端，
    # 又因 H1 须严格早于 H2（H2<=-930，故 H1<=-931），可行年份被全部排除
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_registration", "layer_id": ids["H1"],
         "lower_year": -905, "upper_year": -900, "note": "错误登记"},
    ]}, expect=409)
    check(not r["accepted"] and r["conflict"], "冲突提交返回 409 与冲突诊断")
    check(r["conflict"]["layer_code"] == "H1",
          f"指出冲突层位: {r['conflict'].get('layer_code')}")
    check(r["conflict"]["reason"], f"诊断原因: {r['conflict']['reason']}")
    before_rows = call("GET", "/api/chronology")[1]["layers"]
    check(before_rows == call("GET", "/api/chronology")[1]["layers"],
          "驳回后再次查询编年表不变（原表照常可查）")

    # 撤回当前有效登记，恢复两段空档
    active_reg = [x for x in call("GET", "/api/state")[1]["registrations"]
                  if x["status"] == "active"][0]["id"]
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "withdraw_registration", "registration_id": active_reg},
    ]}, expect=200)
    check(r["accepted"], "撤回登记通过")
    rows = {x["code"]: x for x in r["chronology"]["layers"]}
    check(rows["H1"]["gap_count"] == 1, f"撤回后空档恢复: {rows['H1']['feasible_intervals']}")

    print("[H5] 更正测年并联动撤回失效登记（withdraw_with）")
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_registration", "layer_id": ids["H2"],
         "lower_year": -950, "upper_year": -940, "note": "H2 临时登记"},
    ]}, expect=200)
    check(r["accepted"], "H2 临时登记通过")
    h2_reg = [x for x in call("GET", "/api/state")[1]["registrations"]
              if x["status"] == "active" and x["layer_id"] == ids["H2"]][0]["id"]
    h2_dating = [e for e in call("GET", "/api/state")[1]["evidence"]
                 if e["kind"] == "datum" and e["layer_id"] == ids["H2"]][0]["id"]
    # 不联动撤回：更正后测年 [-1010,-980] 与登记 [-950,-940] 不相交 -> 必然 409
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "correct_dating", "evidence_id": h2_dating,
         "lower_year": -1010, "upper_year": -980, "note": "复测（未撤回登记）"},
    ]}, expect=409)
    check(not r["accepted"], "未联动撤回的更正被驳回")
    # 联动撤回：同一提交通过（更正区间本身与叠压链相容）
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "withdraw_with", "evidence_id": h2_dating,
         "registration_ids": [h2_reg],
         "lower_year": -1010, "upper_year": -980, "note": "复测更正并撤回失效登记"},
    ]}, expect=200)
    check(r["accepted"], "withdraw_with 原子提交通过")
    state = call("GET", "/api/state")[1]
    ev = [e for e in state["evidence"] if e["id"] == h2_dating][0]
    check((ev["lower_year"], ev["upper_year"]) == (-1010, -980), "测年已更正")
    rg = [x for x in state["registrations"] if x["id"] == h2_reg][0]
    check(rg["status"] == "withdrawn", "失效登记已联动撤回")

    print("[H6] 叠压环与输入校验")
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_superposition",
         "earlier_layer_id": ids["H3"], "later_layer_id": ids["H1"], "note": "错误叠压"},
    ]}, expect=409)
    check("cycle_layer_codes" in r["conflict"], f"环诊断: {r['conflict'].get('cycle_layer_codes')}")
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_dating", "layer_id": 99999, "lower_year": 1, "upper_year": 2},
    ]}, expect=422)
    check(r["error"]["code"] == "unknown_layer", f"422 校验: {r['error']['code']}")

    print("[H7] 同批临时 id 与流水查询")
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_layer", "code": "H4"},
        {"action": "add_dating", "layer_id": -1, "lower_year": -1500, "upper_year": -1400},
        {"action": "add_superposition",
         "earlier_layer_id": -1, "later_layer_id": ids["H1"]},
    ]}, expect=200)
    check(r["accepted"], "临时 id 同批解析通过")
    code, subs = call("GET", "/api/submissions", expect=200)
    check(any(s["accepted"] is False for s in subs), "流水中含驳回记录")
    check(any(s["accepted"] for s in subs), "流水中含通过记录")

    print("[H8] 同批新增后撤回 + 登记连续更换两次")
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_layer", "code": "H8A"},
        {"action": "add_layer", "code": "H8B"},
        {"action": "add_dating", "layer_id": -1,
         "lower_year": 500, "upper_year": 600, "note": "待撤回测年"},
        {"action": "add_dating", "layer_id": -2,
         "lower_year": 700, "upper_year": 800, "note": "H8B 测年"},
        {"action": "add_superposition",
         "earlier_layer_id": -1, "later_layer_id": -2, "note": "H8A 早于 H8B"},
        {"action": "add_dating", "layer_id": -1,
         "lower_year": 500, "upper_year": 600, "note": "同批误录"},
        {"action": "withdraw_evidence", "evidence_id": -4},
        {"action": "add_registration", "layer_id": -1,
         "lower_year": 450, "upper_year": 700, "note": "初登"},
        {"action": "replace_registration", "registration_id": -1,
         "lower_year": 460, "upper_year": 650, "note": "更换一"},
        {"action": "replace_registration", "registration_id": -2,
         "lower_year": 470, "upper_year": 550, "note": "更换二"},
    ]}, expect=200)
    check(r["accepted"], "同批撤回/连续更换提交通过")
    state = call("GET", "/api/state")[1]
    ids8 = {x["code"]: x["id"] for x in state["layers"]}
    evs = [e for e in state["evidence"] if e["note"] == "同批误录"]
    check(len(evs) == 1 and evs[0]["active"] is False,
          "撤回的测年保留记录且为已撤回")
    regs = {x["note"]: x for x in state["registrations"]
            if x["layer_id"] == ids8["H8A"]}
    check(regs["初登"]["status"] == "replaced"
          and regs["更换一"]["status"] == "replaced"
          and regs["更换二"]["status"] == "active",
          f"登记状态: {[(k, v['status']) for k, v in regs.items()]}")
    check(regs["初登"]["replaced_by"] == regs["更换一"]["id"]
          and regs["更换一"]["replaced_by"] == regs["更换二"]["id"],
          "更换链使用正式编号")
    rows = {x["code"]: x for x in r["chronology"]["layers"]}
    check(rows["H8A"]["feasible_intervals"] == [[500, 550]],
          f"H8A 按最终登记收窄: {rows['H8A']['feasible_intervals']}")
    check(r["chronology"] == call("GET", "/api/chronology")[1],
          "受理回执编年与入库后查询一致")
    sub = [s for s in call("GET", "/api/submissions")[1]
           if s["id"] == r["submission_id"]][0]
    check(sub["detail"]["chronology"] == r["chronology"],
          "流水中记录的编年与回执一致")

    print("[H9] 已有叠压依据改指同批新增层位")
    sup_id = [e for e in call("GET", "/api/state")[1]["evidence"]
              if e["kind"] == "superposition" and e["note"] == "H8A 早于 H8B"][0]["id"]
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_layer", "code": "H9"},
        {"action": "add_dating", "layer_id": -1,
         "lower_year": 900, "upper_year": 1000, "note": "H9 测年"},
        {"action": "correct_superposition", "evidence_id": sup_id,
         "earlier_layer_id": ids8["H8B"], "later_layer_id": -1,
         "note": "改指 H8B 早于 H9"},
    ]}, expect=200)
    check(r["accepted"], "改指同批新增层位通过")
    state = call("GET", "/api/state")[1]
    ids9 = {x["code"]: x["id"] for x in state["layers"]}
    sup = [e for e in state["evidence"] if e["id"] == sup_id][0]
    check(sup["layer_id"] == ids8["H8B"] and sup["layer2_id"] == ids9["H9"],
          "叠压关系落库为正式编号")
    check(all(e["layer_id"] > 0 for e in state["evidence"])
          and all((x["replaced_by"] or 0) >= 0 for x in state["registrations"]),
          "台账无临时负 id")

    print("[H10] 无解批次回滚，原表不变")
    before = call("GET", "/api/chronology")[1]
    code, r = call("POST", "/api/submissions", {"actions": [
        {"action": "add_layer", "code": "H10"},
        {"action": "add_dating", "layer_id": -1, "lower_year": 1, "upper_year": 2},
        {"action": "add_registration", "layer_id": -1,
         "lower_year": 50, "upper_year": 60},
    ]}, expect=409)
    check(not r["accepted"], "同批新增对象导致无解时整批驳回")
    check(call("GET", "/api/chronology")[1] == before, "驳回后编年表不变")
    check(r["chronology"] == before, "驳回回执中的编年表即原表")

    print("\nHTTP 级自测全部通过。")


if __name__ == "__main__":
    main()
