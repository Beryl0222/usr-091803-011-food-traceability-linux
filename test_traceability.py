"""领域测试：覆盖需求陈述中的每一条承诺。"""

import unittest

from traceability import (
    ConflictError,
    EVENT_DISPOSAL,
    EVENT_HARVEST,
    EVENT_INSPECTION,
    EVENT_REPACK,
    EVENT_RETURN,
    EVENT_SALE,
    EVENT_TEMPERATURE,
    AuthorizationError,
    NotFoundError,
    RESULT_FAIL,
    RESULT_PASS,
    ROLE_AGRICULTURE,
    ROLE_ENTERPRISE,
    ROLE_HEALTH,
    ROLE_INSPECTOR,
    ROLE_MARKET,
    ROLE_PLATFORM,
    ROLE_PUBLIC,
    STATUS_CONFIRMED,
    STATUS_DRAFT,
    STATUS_SUPERSEDED,
    TraceabilityService,
    ValidationError,
)


def role(name, party=None):
    return {"role": name, "party": party or name}


FARM = role(ROLE_AGRICULTURE, "农业局A")
ENTERPRISE = role(ROLE_ENTERPRISE, "某食品公司")
CARRIER = role(ROLE_ENTERPRISE, "某冷链公司")
MARKET = role(ROLE_MARKET, "市场监管局B")
HEALTH = role(ROLE_HEALTH, "卫健委C")
PLATFORM = role(ROLE_PLATFORM, "外卖平台D")
INSPECTOR = role(ROLE_INSPECTOR, "执法支队E")


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.svc = TraceabilityService(clock=lambda: "2026-09-01T10:00:00+00:00")

    # ------------------------------------------------------------------
    # 1. 箱、托盘、散装批次在拆分、混装后仍保有来源关系
    # ------------------------------------------------------------------

    def test_split_and_blend_keeps_origin_relationships(self):
        # 两个产地的散装批次
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "B1", "product": "菠菜", "quantity": 100, "unit": "kg",
             "packaging": "bulk", "farm": "绿源农场", "producer": "绿源合作社"},
            occurred_at="2026-08-20T06:00:00+00:00",
        )
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "B2", "product": "菠菜", "quantity": 300, "unit": "kg",
             "packaging": "bulk", "farm": "青禾农场", "producer": "青禾合作社"},
            occurred_at="2026-08-20T07:00:00+00:00",
        )
        # 混装：25kg B1 + 75kg B2 -> 托盘 P1（100kg）
        self.svc.repack(
            ENTERPRISE,
            inputs=[{"lot": "B1", "consumed": 25}, {"lot": "B2", "consumed": 75}],
            outputs=[{"lot": "P1", "quantity": 100, "packaging": "pallet"}],
            location="分装仓W1", occurred_at="2026-08-21T08:00:00+00:00",
        )
        # 托盘拆成 40 个 2.5kg 箱
        outputs = [{"lot": f"C{i:02d}", "quantity": 2.5, "packaging": "case"} for i in range(40)]
        self.svc.repack(
            ENTERPRISE, inputs=[{"lot": "P1", "consumed": 100}], outputs=outputs,
            location="分装仓W1", occurred_at="2026-08-21T09:00:00+00:00",
        )

        origin = self.svc.trace_origin("C07")
        shares = {r["lot"]: r["share"] for r in origin["roots"]}
        self.assertAlmostEqual(shares["B1"], 0.25, places=6)
        self.assertAlmostEqual(shares["B2"], 0.75, places=6)
        self.assertEqual({r["farm"] for r in origin["roots"]}, {"绿源农场", "青禾农场"})

        # 库存守恒：原批次剩余量保留
        self.assertAlmostEqual(self.svc.get_lot("B1")["quantity"], 75.0)
        self.assertAlmostEqual(self.svc.get_lot("B2")["quantity"], 225.0)

    def test_repack_rejects_non_conserved_quantities(self):
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "B1", "product": "菠菜", "quantity": 10, "unit": "kg",
             "packaging": "bulk", "farm": "f", "producer": "p"},
        )
        with self.assertRaises(ValidationError):
            self.svc.repack(ENTERPRISE, inputs=[{"lot": "B1", "consumed": 10}],
                            outputs=[{"lot": "X", "quantity": 9, "packaging": "case"}])

    # ------------------------------------------------------------------
    # 2. 接收七类事件
    # ------------------------------------------------------------------

    def test_all_seven_event_types_accepted(self):
        self.svc.submit_event(EVENT_HARVEST, FARM,
                              {"lot": "L1", "product": "鱼", "quantity": 50, "unit": "kg",
                               "packaging": "case", "farm": "渔场", "producer": "渔业社"},
                              occurred_at="2026-08-20T00:00:00+00:00")
        self.svc.submit_event(EVENT_TEMPERATURE, CARRIER,
                              {"lot": "L1", "vehicle": "冷B-123", "temperature": -17.5,
                               "condition": "ok", "destination": "仓W1", "carrier": "某冷链公司"},
                              occurred_at="2026-08-20T03:00:00+00:00")
        self.svc.repack(ENTERPRISE, inputs=[{"lot": "L1", "consumed": 50}],
                        outputs=[{"lot": "L2", "quantity": 50, "packaging": "bulk"}],
                        occurred_at="2026-08-20T05:00:00+00:00")
        self.svc.submit_event(EVENT_INSPECTION, MARKET,
                              {"lot": "L2", "lab": "检测中心", "readings": {"镉": 0.05}},
                              occurred_at="2026-08-20T06:00:00+00:00")
        self.svc.submit_event(EVENT_SALE, PLATFORM,
                              {"lot": "L2", "location": "门店S1", "buyer": "门店S1"},
                              occurred_at="2026-08-20T08:00:00+00:00")
        self.svc.submit_event(EVENT_RETURN, ENTERPRISE,
                              {"lot": "L2", "reason": "包装破损", "received_by": "某食品公司",
                               "location": "仓W1"},
                              occurred_at="2026-08-20T10:00:00+00:00")
        self.svc.submit_event(EVENT_DISPOSAL, ENTERPRISE,
                              {"lot": "L2", "method": "高温化制", "operator": "无害化处理厂",
                               "reason_code": "R3", "location": "处理厂D1"},
                              occurred_at="2026-08-20T12:00:00+00:00")
        types = [e["type"] for e in self.svc.list_events()]
        self.assertEqual(
            sorted(types),
            sorted(["harvest", "temperature", "repack", "inspection", "sale", "return", "disposal"]),
        )

    # ------------------------------------------------------------------
    # 3. 跨部门重复上报同一事实只形成一条可核对记录
    # ------------------------------------------------------------------

    def test_duplicate_fact_from_multiple_parties_merges(self):
        payload = {"lot": "L1", "product": "菜", "quantity": 10, "unit": "kg",
                   "packaging": "bulk", "farm": "f", "producer": "p"}
        e1, d1 = self.svc.submit_event(EVENT_HARVEST, FARM, payload)
        self.assertFalse(d1)
        # 平台、市场监管、卫生重复上报同一事实（字段顺序、微小数值误差不影响）
        same = dict(payload)
        same["irrelevant_note"] = "平台转述"
        e2, d2 = self.svc.submit_event(EVENT_HARVEST, PLATFORM, same, reporter_ref="平台单据#9")
        e3, d3 = self.svc.submit_event(EVENT_HARVEST, MARKET, payload, reporter_ref="市监单据#2")
        e4, d4 = self.svc.submit_event(EVENT_HARVEST, HEALTH, payload)
        self.assertTrue(all((d2, d3, d4)))
        self.assertEqual({e1["event_id"], e2["event_id"], e3["event_id"], e4["event_id"]},
                         {e1["event_id"]})
        parties = {r["party"] for r in e1["reporters"]}
        self.assertEqual(parties, {"农业局A", "外卖平台D", "市场监管局B", "卫健委C"})
        self.assertEqual(len(self.svc.list_events()), 1)

    def test_conflicting_facts_are_not_silently_overwritten(self):
        base = {"lot": "L1", "product": "菜", "quantity": 10, "unit": "kg",
                "packaging": "bulk", "farm": "f", "producer": "p"}
        self.svc.submit_event(EVENT_HARVEST, FARM, base)
        e_pass, _ = self.svc.submit_event(
            EVENT_INSPECTION, MARKET,
            {"lot": "L1", "lab": "检测中心", "readings": {"农残": 0.01}, "result": RESULT_PASS},
        )
        e_fail, _ = self.svc.submit_event(
            EVENT_INSPECTION, HEALTH,
            {"lot": "L1", "lab": "复检实验室", "readings": {"农残": 0.80}, "result": RESULT_FAIL},
        )
        # 两条记录都在，且互相登记冲突；带冲突不可确认
        self.assertEqual(len(self.svc.list_events(EVENT_INSPECTION)), 2)
        self.assertTrue(e_pass["versions"] and e_fail["versions"])
        pending = {p["event_id"] for p in self.svc.pending_conflicts()}
        self.assertEqual(pending, {e_pass["event_id"], e_fail["event_id"]})
        with self.assertRaises(ConflictError):
            self.svc.confirm_event(e_pass["event_id"], MARKET)

        # 显式裁决：采纳不合格结论，另一条被取代
        resolved = self.svc.resolve_conflict(
            e_fail["event_id"], MARKET,
            {"lot": "L1", "lab": "复检实验室", "readings": {"农残": 0.80}, "result": RESULT_FAIL},
            reason="复检方法更可靠",
        )
        self.assertEqual(resolved["status"], STATUS_CONFIRMED)
        self.assertEqual(self.svc.get_event(e_pass["event_id"])["status"], STATUS_SUPERSEDED)
        self.assertEqual(self.svc.pending_conflicts(), [])

    # ------------------------------------------------------------------
    # 4. 企业只可修改尚未确认的草稿
    # ------------------------------------------------------------------

    def test_enterprise_may_only_revise_unconfirmed_draft(self):
        other_company = role(ROLE_ENTERPRISE, "其他公司")
        e, _ = self.svc.submit_event(
            EVENT_HARVEST, ENTERPRISE,
            {"lot": "L1", "product": "菜", "quantity": 10, "unit": "kg",
             "packaging": "bulk", "farm": "f", "producer": "p"},
        )
        self.assertEqual(e["status"], STATUS_DRAFT)
        # 非企业不能改
        with self.assertRaises(AuthorizationError):
            self.svc.revise_event(e["event_id"], FARM, e["payload"])
        # 非本企业不能改
        with self.assertRaises(AuthorizationError):
            self.svc.revise_event(e["event_id"], other_company, e["payload"])
        # 本企业可以改
        revised = self.svc.revise_event(e["event_id"], ENTERPRISE, {**e["payload"], "quantity": 9})
        self.assertEqual(revised["payload"]["quantity"], 9)
        # 确认后冻结
        self.svc.confirm_event(e["event_id"], ENTERPRISE)
        with self.assertRaises(AuthorizationError):
            self.svc.revise_event(e["event_id"], ENTERPRISE, {**e["payload"], "quantity": 8})

    # ------------------------------------------------------------------
    # 5. 新标准按生效时点适用，不影响已结案判断
    # ------------------------------------------------------------------

    def test_standards_apply_by_effective_time(self):
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "L1", "product": "菜", "quantity": 10, "unit": "kg",
             "packaging": "bulk", "farm": "f", "producer": "p"},
        )
        insp, _ = self.svc.submit_event(
            EVENT_INSPECTION, MARKET,
            {"lot": "L1", "lab": "检测中心", "readings": {"农残": 0.30}},
            occurred_at="2026-06-01T00:00:00+00:00",
        )
        # 旧标准：限量 0.5，判定合格
        self.svc.publish_standard(
            "STD-v1", "旧限量", {"农残": 0.5}, effective_at="2026-01-01T00:00:00+00:00",
        )
        before = self.svc.evaluate_inspection(insp["event_id"])
        self.assertEqual(before["verdict"], RESULT_PASS)
        self.assertEqual(before["standard_id"], "STD-v1")

        # 先结案，固化合格判断
        case = self.svc.close_case("CASE-1", "六月例行检查", {"L1"}, INSPECTOR,
                                   closed_at="2026-06-02T00:00:00+00:00")
        snap = next(s for s in case["events"] if s["type"] == EVENT_INSPECTION)
        self.assertEqual(snap["evaluation"]["verdict"], RESULT_PASS)

        # 新标准 2026-09-01 生效，限量收紧到 0.1
        self.svc.publish_standard(
            "STD-v2", "新限量", {"农残": 0.1}, effective_at="2026-09-01T00:00:00+00:00",
            supersedes="STD-v1",
        )
        # 同一历史事件按发生时点仍适用旧标准
        after = self.svc.evaluate_inspection(insp["event_id"])
        self.assertEqual(after["standard_id"], "STD-v1")
        self.assertEqual(after["verdict"], RESULT_PASS)
        # 结案快照未被倒改
        frozen = self.svc.get_case("CASE-1")
        self.assertEqual(
            next(s for s in frozen["events"] if s["type"] == EVENT_INSPECTION)["evaluation"]["verdict"],
            RESULT_PASS,
        )
        # 但新标准生效后的检验按新标准判定不合格
        insp2, _ = self.svc.submit_event(
            EVENT_INSPECTION, MARKET,
            {"lot": "L1", "lab": "检测中心", "readings": {"农残": 0.30}},
            occurred_at="2026-09-02T00:00:00+00:00",
        )
        self.assertEqual(self.svc.evaluate_inspection(insp2["event_id"])["verdict"], RESULT_FAIL)

    def test_closed_case_cannot_be_retroactively_changed(self):
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "L1", "product": "菜", "quantity": 10, "unit": "kg",
             "packaging": "bulk", "farm": "f", "producer": "p"},
        )
        e_pass, _ = self.svc.submit_event(
            EVENT_INSPECTION, MARKET,
            {"lot": "L1", "lab": "检测中心", "readings": {"农残": 0.01}, "result": RESULT_PASS},
        )
        self.svc.confirm_event(e_pass["event_id"], MARKET)
        self.svc.close_case("CASE-9", "已结案件", {"L1"}, INSPECTOR)
        e_fail, _ = self.svc.submit_event(
            EVENT_INSPECTION, HEALTH,
            {"lot": "L1", "lab": "复检实验室", "readings": {"农残": 0.80}, "result": RESULT_FAIL},
        )
        # 结案后不得通过冲突裁决倒改，只能以新事件更正
        with self.assertRaises(AuthorizationError):
            self.svc.resolve_conflict(e_pass["event_id"], MARKET, e_fail["payload"], "试图翻案")
        with self.assertRaises(AuthorizationError):
            self.svc.revise_event(e_pass["event_id"], ENTERPRISE, e_fail["payload"])

    # ------------------------------------------------------------------
    # 6. 召回按生效时点圈定范围
    # ------------------------------------------------------------------

    def test_recall_scope_uses_effective_time_and_excludes_disposed(self):
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "B1", "product": "莓", "quantity": 100, "unit": "kg",
             "packaging": "bulk", "farm": "f", "producer": "p"},
            occurred_at="2026-08-01T00:00:00+00:00",
        )
        self.svc.repack(ENTERPRISE, inputs=[{"lot": "B1", "consumed": 100}],
                        outputs=[{"lot": "C1", "quantity": 50, "packaging": "case"},
                                 {"lot": "C2", "quantity": 50, "packaging": "case"}],
                        occurred_at="2026-08-02T00:00:00+00:00")
        self.svc.submit_event(EVENT_SALE, PLATFORM, {"lot": "C1", "location": "门店S1", "buyer": "门店S1"},
                              occurred_at="2026-08-03T00:00:00+00:00")
        # C2 在召回生效前已销毁（销毁经市场监管确认）
        d2, _ = self.svc.submit_event(EVENT_DISPOSAL, ENTERPRISE,
                                      {"lot": "C2", "method": "销毁", "operator": "处理厂", "location": "D1"},
                                      occurred_at="2026-08-04T00:00:00+00:00")
        self.svc.confirm_event(d2["event_id"], MARKET)
        recall = self.svc.issue_recall(
            "RC-1", "农残超标", ["B1"], effective_at="2026-08-10T00:00:00+00:00",
            reporter=MARKET,
        )
        scoped = {s["lot"]: s["disposition"] for s in recall["scope"]}
        # B1 已全部投入分装（库存为 0），仍作为来源列入；C1 已售在召回范围内
        self.assertIn("C1", scoped)
        self.assertEqual(scoped["C1"], "sold")
        # C2 生效时点前已销毁，排除并留痕
        self.assertEqual({s["lot"] for s in recall["excluded_disposed"]}, {"C2"})

    # ------------------------------------------------------------------
    # 7. 门店失联扫码：恢复后回放，冲突显式处理
    # ------------------------------------------------------------------

    def test_offline_scan_replays_and_conflict_is_explicit(self):
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "C1", "product": "肉", "quantity": 10, "unit": "kg",
             "packaging": "case", "farm": "f", "producer": "p"},
            occurred_at="2026-08-01T00:00:00+00:00",
        )
        # 失联期间（8-05）门店扫到 C1；但账本显示 C1 已于 8-04 确认销毁
        disposal, _ = self.svc.submit_event(EVENT_DISPOSAL, ENTERPRISE,
                                            {"lot": "C1", "method": "销毁", "operator": "处理厂", "location": "D1"},
                                            occurred_at="2026-08-04T00:00:00+00:00")
        self.svc.confirm_event(disposal["event_id"], MARKET)
        self.svc.buffer_scan("SC-1", store="门店S9", lot_id="C1",
                             scanned_at="2026-08-05T12:00:00+00:00",
                             recovered_at="2026-08-06T09:00:00+00:00")
        with self.assertRaises(ConflictError):
            self.svc.replay_scan("SC-1", PLATFORM)
        # 冲突挂起，原始扫码与销毁记录均保留，且未凭空生成销售
        scan = self.svc.scan_records["SC-1"]
        self.assertEqual(scan["state"], "conflict")
        disposal = next(e for e in self.svc.list_events(EVENT_DISPOSAL) if e["lot"] == "C1")
        self.assertTrue(any(v.get("scan_id") == "SC-1" for v in disposal["versions"]))
        self.assertFalse(any(e["lot"] == "C1" and e["type"] == EVENT_SALE
                             for e in self.svc.list_events()))

        # 执法端显式处理：确认销毁后仍在售 -> 自动封存，去向转入已封存
        result = self.svc.resolve_scan_conflict("SC-1", INSPECTOR, "product_located",
                                                note="现场查扣同批次产品")
        self.assertTrue(result["seizure_event"])
        to_seize = self.svc.still_to_seize(["C1"])
        self.assertEqual(to_seize, [])
        # 已处理的冲突不再出现在未决列表
        disposal = self.svc.get_event(disposal["event_id"])
        self.assertFalse(any(v.get("state") == "open" for v in disposal["versions"]))

    def test_clean_offline_scan_replays_as_sale(self):
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "C1", "product": "肉", "quantity": 10, "unit": "kg",
             "packaging": "case", "farm": "f", "producer": "p"},
            occurred_at="2026-08-01T00:00:00+00:00",
        )
        self.svc.buffer_scan("SC-2", store="门店S1", lot_id="C1",
                             scanned_at="2026-08-05T12:00:00+00:00")
        event = self.svc.replay_scan("SC-2", PLATFORM)
        self.assertEqual(event["type"], EVENT_SALE)
        self.assertEqual(event["payload"]["location"], "门店S1")
        self.assertEqual(self.svc.scan_records["SC-2"]["state"], "replayed")

    # ------------------------------------------------------------------
    # 8. 公众脱敏安全摘要
    # ------------------------------------------------------------------

    def test_public_summary_is_masked(self):
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "C1", "product": "牛奶", "quantity": 10, "unit": "L",
             "packaging": "case", "farm": "牧场", "producer": "某乳业公司"},
        )
        insp, _ = self.svc.submit_event(
            EVENT_INSPECTION, MARKET, {"lot": "C1", "lab": "检测中心", "readings": {"菌落总数": 1.0}},
        )
        self.svc.publish_standard("STD-milk", "乳标准", {"菌落总数": 5.0},
                                  effective_at="2026-01-01T00:00:00+00:00")
        summary = self.svc.public_summary("C1")
        self.assertNotIn("C1", repr(summary))          # 原始批次码不出现
        self.assertNotIn("某乳业公司", repr(summary))   # 责任主体不出现
        self.assertEqual(summary["product"], "牛奶")
        self.assertEqual(summary["safety"], "checked")
        self.assertTrue(summary["inspections"])
        self.assertIsNone(summary["owner"])

    def test_public_summary_flags_unsafe(self):
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "C1", "product": "牛奶", "quantity": 10, "unit": "L",
             "packaging": "case", "farm": "牧场", "producer": "乳业"},
        )
        insp, _ = self.svc.submit_event(
            EVENT_INSPECTION, MARKET, {"lot": "C1", "lab": "检测中心", "readings": {"毒素": 9}},
        )
        self.svc.publish_standard("STD-x", "标准", {"毒素": 1},
                                  effective_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(self.svc.public_summary("C1")["safety"], "unsafe")

    # ------------------------------------------------------------------
    # 9. 执法：从消费凭证逆向导出责任主体、适用标准、证据链与封存清单
    # ------------------------------------------------------------------

    def _build_full_chain(self):
        # 产地 -> 冷链 -> 分装 -> 检验 -> 销售（消费凭证）-> 部分退回销毁
        self.svc.submit_event(
            EVENT_HARVEST, FARM,
            {"lot": "B1", "product": "冻虾", "quantity": 100, "unit": "kg",
             "packaging": "bulk", "farm": "海湾养殖场", "producer": "海湾水产合作社"},
            occurred_at="2026-08-10T06:00:00+00:00",
        )
        self.svc.submit_event(
            EVENT_TEMPERATURE, CARRIER,
            {"lot": "B1", "vehicle": "冷A-001", "temperature": -18.0, "condition": "ok",
             "destination": "分装仓W1", "carrier": "某冷链公司"},
            occurred_at="2026-08-10T10:00:00+00:00",
        )
        self.svc.repack(
            ENTERPRISE, inputs=[{"lot": "B1", "consumed": 100}],
            outputs=[{"lot": "C1", "quantity": 50, "packaging": "case"},
                     {"lot": "C2", "quantity": 50, "packaging": "case"}],
            location="分装仓W1", occurred_at="2026-08-11T08:00:00+00:00",
        )
        self.svc.publish_standard("STD-seafood", "水产品限量", {"重金属": 0.5},
                                  effective_at="2026-01-01T00:00:00+00:00")
        for lot in ("C1", "C2"):
            insp, _ = self.svc.submit_event(
                EVENT_INSPECTION, MARKET,
                {"lot": lot, "lab": "食品检测中心", "readings": {"重金属": 0.2}},
                occurred_at="2026-08-11T09:00:00+00:00",
            )
            self.svc.confirm_event(insp["event_id"], MARKET)
        self.svc.submit_event(EVENT_SALE, PLATFORM,
                              {"lot": "C1", "location": "门店S1", "buyer": "门店S1"},
                              occurred_at="2026-08-12T08:00:00+00:00")
        self.svc.submit_event(EVENT_SALE, PLATFORM,
                              {"lot": "C2", "location": "门店S2", "buyer": "门店S2"},
                              occurred_at="2026-08-12T09:00:00+00:00")
        self.svc.register_consumer_sale(
            "RCP-20260812-001", ["C1"], store="门店S1", sold_at="2026-08-12T12:00:00+00:00",
            reporter=PLATFORM,
        )

    def test_inspector_reverse_investigation_from_receipt(self):
        self._build_full_chain()
        report = self.svc.investigate_receipt("RCP-20260812-001", INSPECTOR)
        # 责任主体：产地生产者、冷链、分装企业、销售门店/平台都在
        parties = set(report["responsible_parties"])
        self.assertIn("海湾水产合作社", parties)
        self.assertIn("某冷链公司", parties)
        self.assertIn("门店S1", parties)
        # 适用标准
        standards = {s["standard_id"] for s in report["applicable_standards"]}
        self.assertIn("STD-seafood", standards)
        # 完整证据链按时间排列，从采收一路到销售
        chain_types = [e["type"] for e in report["evidence_chain"]]
        self.assertEqual(chain_types[0], EVENT_HARVEST)
        self.assertIn(EVENT_TEMPERATURE, chain_types)
        self.assertIn(EVENT_REPACK, chain_types)
        self.assertIn(EVENT_INSPECTION, chain_types)
        self.assertEqual(chain_types[-1], EVENT_SALE)
        # 仍需封存的去向：C1 已售未销毁未封存
        lots = {row["lot"] for row in report["still_to_seize"]}
        self.assertIn("C1", lots)
        self.assertNotIn("C2", lots)  # C2 不在本凭证购买范围

    def test_seizure_list_shrinks_after_action(self):
        self._build_full_chain()
        before = {row["lot"] for row in self.svc.still_to_seize(["C1"])}
        self.assertIn("C1", before)
        self.svc.seize("C1", MARKET, reason="消费者投诉疑似中毒", case_id="CASE-X",
                       occurred_at="2026-08-12T13:00:00+00:00")
        after = self.svc.still_to_seize(["C1"])
        self.assertEqual(after, [])

    def test_disposed_destination_not_in_seizure_list(self):
        self._build_full_chain()
        self.svc.submit_event(EVENT_RETURN, ENTERPRISE,
                              {"lot": "C1", "reason": "召回退回", "received_by": "某食品公司"},
                              occurred_at="2026-08-13T00:00:00+00:00")
        disposal_draft, _ = self.svc.submit_event(
            EVENT_DISPOSAL, ENTERPRISE,
            {"lot": "C1", "method": "化制", "operator": "处理厂"},
            occurred_at="2026-08-13T06:00:00+00:00",
        )
        # 销毁尚为草稿时，封存义务不免除
        self.assertIn("C1", {row["lot"] for row in self.svc.still_to_seize(["C1"])})
        self.svc.confirm_event(disposal_draft["event_id"], MARKET)
        # 确认后才从封存清单消失
        self.assertEqual(self.svc.still_to_seize(["C1"]), [])

    def test_non_inspector_cannot_investigate(self):
        self._build_full_chain()
        with self.assertRaises(AuthorizationError):
            self.svc.investigate_receipt("RCP-20260812-001", role(ROLE_PUBLIC, "路人"))
        with self.assertRaises(AuthorizationError):
            self.svc.investigate_receipt("RCP-20260812-001", PLATFORM)

    def test_unknown_receipt_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.svc.investigate_receipt("nope", INSPECTOR)


if __name__ == "__main__":
    unittest.main()
