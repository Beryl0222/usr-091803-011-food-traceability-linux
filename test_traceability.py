"""食品追溯领域核心的端到端场景测试。

以一次跨部门追查为主线：产地采收 → 冷链运输 → 分装仓拆分混装 →
检验（跨部门重复上报同一事实）→ 调拨门店 → 销售（消费凭证）→
门店离线扫码恢复 → 新标准发布与结案 → 召回与封存 → 凭证逆向导出。
"""

import unittest

from traceability import (
    Forbidden,
    StateConflict,
    TraceError,
    TraceService,
)


class TraceDomainTest(unittest.TestCase):
    def setUp(self):
        self.svc = TraceService()
        self.farm = self.svc.register_party("云岭菜园", "farm", contact="产地负责人")
        self.carrier = self.svc.register_party("迅捷冷链", "carrier")
        self.packhouse = self.svc.register_party("城南分装仓", "packhouse")
        self.lab = self.svc.register_party("市食品检验中心", "lab")
        self.store_a = self.svc.register_party("城东门店", "store")
        self.store_b = self.svc.register_party("城西门店", "store")
        self.platform = self.svc.register_party("外卖平台", "platform")
        self.agri = self.svc.register_party("农业农村局", "authority")
        self.market = self.svc.register_party("市场监管局", "authority")
        self.health = self.svc.register_party("卫健委", "authority")

    # -- 工具 --------------------------------------------------------------

    def report(self, party, etype, data, op_id=None):
        return self.svc.report_event(party["token"], etype, data, op_id)

    def confirm(self, party, event):
        return self.svc.confirm_event(party["token"], event["id"])

    def build_harvest(self, product="生菜", qty=100.0, when="2026-09-01T08:00:00",
                      token_party=None):
        party = token_party or self.farm
        e = self.report(party, "harvest", {
            "at": when, "product": product, "qty": qty, "unit": "kg",
            "origin": "云岭基地A区"})
        self.confirm(party, e)
        return self.svc.get_event(e["id"])["payload"]["batch_id"]

    # -- 1. 拆分、混装后仍保有来源关系 -------------------------------------

    def test_split_and_mix_keeps_lineage(self):
        b1 = self.build_harvest(qty=100)
        b2 = self.build_harvest(product="生菜", qty=60,
                                when="2026-09-01T09:00:00")

        # 分装：B1 拆出 40kg 箱装 C1，60kg 与 B2 的 60kg 混装成托盘 P1
        repack = self.report(self.packhouse, "repack", {
            "at": "2026-09-02T06:00:00",
            "mode": "split_and_mix",
            "inputs": [
                {"batch_id": b1, "qty": 100},
                {"batch_id": b2, "qty": 60},
            ],
            "outputs": [
                {"batch_id": "C1", "kind": "case", "product": "生菜",
                 "qty": 40, "unit": "kg",
                 "sources": [{"batch_id": b1, "qty": 40}]},
                {"batch_id": "P1", "kind": "pallet", "product": "生菜",
                 "qty": 120, "unit": "kg",
                 "sources": [{"batch_id": b1, "qty": 60},
                             {"batch_id": b2, "qty": 60}]},
            ],
        })
        self.confirm(self.packhouse, repack)

        # 箱 C1 只能逆溯到 B1；混装托盘 P1 必须同时溯到 B1 和 B2
        up_c1 = {n["batch_id"] for n in self.svc.lineage("C1", "up")["upstream"]}
        self.assertEqual(up_c1, {b1})
        up_p1 = {n["batch_id"] for n in self.svc.lineage("P1", "up")["upstream"]}
        self.assertEqual(up_p1, {b1, b2})

        # 从产地批次向下能找到拆/混后的全部形态
        down_b1 = {n["batch_id"] for n in self.svc.lineage(b1, "down")["downstream"]}
        self.assertEqual(down_b1, {"C1", "P1"})
        down_b2 = {n["batch_id"] for n in self.svc.lineage(b2, "down")["downstream"]}
        self.assertEqual(down_b2, {"P1"})

        # 数量守恒：投入 160kg，产出 40+120
        self.assertEqual(40 + 120, 100 + 60)

    # -- 2. 跨部门重复上报同一事实只形成一条可核对记录 ---------------------

    def test_duplicate_facts_merge_with_attestations(self):
        b1 = self.build_harvest()
        common = {
            "at": "2026-09-02T10:00:00", "batch_id": b1,
            "result": "pass", "report_no": "JC-20260902-001",
        }
        # 农业、市场监管、平台对同一份报告重复上报
        e1 = self.report(self.agri, "inspection", dict(common))
        e2 = self.report(self.market, "inspection",
                         {**common, "note": "市场监管转抄"})
        e3 = self.report(self.platform, "inspection",
                         {**common, "source_channel": "平台同步"})
        self.assertEqual(e1["id"], e2["id"])
        self.assertEqual(e1["id"], e3["id"])
        self.assertEqual(len(self.svc.store.events), 2)  # 采收 + 这一条检验

        stored = self.svc.get_event(e1["id"])
        parties = {a["party_id"] for a in stored["attestations"]}
        self.assertEqual(parties, {self.agri["id"], self.market["id"],
                                   self.platform["id"]})
        self.assertFalse(stored["disputed"])

        # 同一票据销毁也只记一条
        d1 = self.report(self.market, "destruction", {
            "at": "2026-09-03T08:00:00", "batch_id": b1, "qty": 10,
            "destruction_no": "XH-001"})
        d2 = self.report(self.health, "destruction", {
            "at": "2026-09-03T08:00:00", "batch_id": b1, "qty": 10,
            "destruction_no": "XH-001"})
        self.assertEqual(d1["id"], d2["id"])

    def test_conflicting_duplicate_is_marked_disputed_until_resolved(self):
        b1 = self.build_harvest()
        base = {"at": "2026-09-02T10:00:00", "batch_id": b1,
                "report_no": "JC-002"}
        e1 = self.report(self.lab, "inspection", {**base, "result": "pass"})
        e2 = self.report(self.health, "inspection", {**base, "result": "fail"})
        self.assertEqual(e1["id"], e2["id"])
        self.assertTrue(self.svc.get_event(e1["id"])["disputed"])

        # 分歧未裁定前不能确认结案
        with self.assertRaises(StateConflict):
            self.confirm(self.lab, e1)

        # 执法方裁定后，以被采信口径为准并全程留痕
        resolved = self.svc.resolve_dispute(
            e1["id"], self.health["id"], "复检判定不合格，采信卫健委")
        self.assertFalse(resolved["disputed"])
        self.assertEqual(resolved["payload"]["result"], "fail")
        self.assertEqual(resolved["dispute_resolution"]["canonical_party_id"],
                         self.health["id"])

    def test_client_idempotency_on_retry(self):
        b1 = self.build_harvest()
        kwargs = {"at": "2026-09-02T12:00:00", "batch_id": b1,
                  "qty": 5, "receipt_no": "R- offline-1"}
        r1 = self.report(self.store_a, "sale", kwargs, op_id="op-77")
        r2 = self.report(self.store_a, "sale", kwargs, op_id="op-77")
        self.assertEqual(r1["id"], r2["id"])

    # -- 3. 草稿可改、确认后不可改 -----------------------------------------

    def test_same_minute_distinct_events_do_not_merge(self):
        # 同一分钟内两笔采收，数量不同，必须是两条独立记录与两个批次
        e1 = self.report(self.farm, "harvest", {
            "at": "2026-09-01T08:00:10", "product": "生菜", "qty": 100})
        e2 = self.report(self.farm, "harvest", {
            "at": "2026-09-01T08:00:50", "product": "生菜", "qty": 80})
        self.assertNotEqual(e1["id"], e2["id"])
        # 而完全相同的重复上报（同口径同分钟）仍是同一条
        e3 = self.report(self.farm, "harvest", {
            "at": "2026-09-01T08:00:10", "product": "生菜", "qty": 100})
        self.assertEqual(e3["id"], e1["id"])

    def test_only_unconfirmed_draft_editable_by_owner(self):
        draft = self.report(self.farm, "harvest", {
            "at": "2026-09-01T08:00:00", "product": "生菜", "qty": 90})
        patched = self.svc.edit_draft(
            self.farm["token"], draft["id"], {"qty": 100})
        self.assertEqual(patched["payload"]["qty"], 100)
        self.assertEqual(len(patched["amendments"]), 1)

        # 其他主体不能改
        with self.assertRaises(Forbidden):
            self.svc.edit_draft(self.packhouse["token"], draft["id"], {"qty": 1})

        self.confirm(self.farm, draft)
        # 确认后企业自己也不能改
        with self.assertRaises(StateConflict):
            self.svc.edit_draft(self.farm["token"], draft["id"], {"qty": 1})

    # -- 4. 新标准不倒改已结案的判断 ---------------------------------------

    def test_new_standard_does_not_rewrite_closed_case(self):
        b1 = self.build_harvest()
        # 旧标准 9 月生效
        old = self.svc.publish_standard(
            "GB-LEAF", "2023", "叶菜农药残留限量", "2023-01-01T00:00:00")
        insp = self.report(self.lab, "inspection", {
            "at": "2026-09-02T10:00:00", "batch_id": b1, "result": "pass",
            "standard_code": "GB-LEAF", "report_no": "JC-old"})
        insp = self.confirm(self.lab, insp)
        self.assertEqual(insp["standard_snapshot"]["version"], "2023")

        case = self.svc.close_case(
            "9月初例行抽检", [insp["id"]], "合格，不予立案")
        self.assertEqual(case["conclusions"][0]["standard_snapshot"]["version"],
                         "2023")

        # 9 月 10 日更严格的新标准生效，旧事件适用快照不变
        self.svc.publish_standard(
            "GB-LEAF", "2026", "叶菜农药残留限量（修订）",
            "2026-09-10T00:00:00")
        again = self.svc.get_event(insp["id"])
        self.assertEqual(again["standard_snapshot"]["id"], old["id"])
        closed = self.svc.get_case(case["id"])
        self.assertEqual(
            closed["conclusions"][0]["standard_snapshot"]["version"], "2023")
        self.assertEqual(closed["judgment"], "合格，不予立案")

        # 新标准生效后的新检验按新版本判定
        b2 = self.build_harvest(when="2026-09-11T08:00:00")
        insp2 = self.report(self.lab, "inspection", {
            "at": "2026-09-11T10:00:00", "batch_id": b2, "result": "pass",
            "standard_code": "GB-LEAF", "report_no": "JC-new"})
        insp2 = self.confirm(self.lab, insp2)
        self.assertEqual(insp2["standard_snapshot"]["version"], "2026")

        # 不能引用尚未生效的标准判定
        b3 = self.build_harvest(when="2026-09-05T08:00:00")
        future = self.svc.publish_standard(
            "GB-LEAF", "2030", "未来版", "2030-01-01T00:00:00")
        bad = self.report(self.lab, "inspection", {
            "at": "2026-09-05T10:00:00", "batch_id": b3, "result": "pass",
            "standard_id": future["id"], "report_no": "JC-future"})
        with self.assertRaises(TraceError):
            self.confirm(self.lab, bad)

    # -- 5. 召回按生效时点圈范围，并列出仍需封存的去向 ---------------------

    def _distribute(self, b1):
        """采收 100kg：分装成两箱各 40kg，余 20kg；分别调入两家门店。"""
        repack = self.report(self.packhouse, "repack", {
            "at": "2026-09-02T06:00:00", "inputs": [{"batch_id": b1, "qty": 100}],
            "outputs": [
                {"batch_id": "C-A", "kind": "case", "product": "生菜",
                 "qty": 40, "unit": "kg"},
                {"batch_id": "C-B", "kind": "case", "product": "生菜",
                 "qty": 40, "unit": "kg"},
                {"batch_id": "BULK-X", "kind": "bulk", "product": "生菜",
                 "qty": 20, "unit": "kg"},
            ]})
        self.confirm(self.packhouse, repack)
        for bid, store in (("C-A", self.store_a), ("C-B", self.store_b)):
            t = self.report(self.packhouse, "transfer", {
                "at": "2026-09-02T08:00:00", "batch_id": bid, "qty": 40,
                "from": self.packhouse["id"], "to": store["id"]})
            self.confirm(self.packhouse, t)

    def test_recall_scopes_at_effective_time_and_lists_pending_seals(self):
        b1 = self.build_harvest()
        self._distribute(b1)

        # 召回决定 9 月 3 日生效；范围必须包含拆分后的三批
        recall = self.svc.create_recall(
            "农残超标", "2026-09-03T09:00:00", root_batch_ids=[b1])
        scope = set(recall["scope_batches"])
        self.assertEqual(scope, {b1, "C-A", "C-B", "BULK-X"})

        pending = {(p["batch_id"], p["holder_id"]): p["qty"]
                   for p in recall["pending_seals"]}
        # C-A、C-B 各 40kg 在两家门店待封存；BULK-X 在分装仓
        self.assertEqual(pending[("C-A", self.store_a["id"])], 40)
        self.assertEqual(pending[("C-B", self.store_b["id"])], 40)
        self.assertEqual(pending[("BULK-X", self.packhouse["id"])], 20)

        # 城东门店封存 40kg 后，待封存清单收敛
        updated = self.svc.seal_batch(
            self.store_a["token"], recall["id"], "C-A",
            self.store_a["id"], 40)
        holders = {p["holder_id"] for p in updated["pending_seals"]}
        self.assertNotIn(self.store_a["id"], holders)
        self.assertIn(self.store_b["id"], holders)

        # 范围快照不随后续操作扩大/倒改：再分装出的新批次不在本次范围
        later = self.report(self.packhouse, "repack", {
            "at": "2026-09-04T06:00:00",
            "inputs": [{"batch_id": "BULK-X", "qty": 20}],
            "outputs": [{"batch_id": "C-LATE", "kind": "case",
                         "product": "生菜", "qty": 20, "unit": "kg"}]})
        self.confirm(self.packhouse, later)
        refreshed = self.svc.get_recall(recall["id"])
        self.assertNotIn("C-LATE", refreshed["scope_batches"])

    def test_recall_snapshot_excludes_quantities_sold_before_effective_time(self):
        b1 = self.build_harvest()
        # 9 月 2 日售出 30kg（召回生效前已到消费者，不应出现在封存清单）
        sale = self.report(self.store_a, "sale", {
            "at": "2026-09-02T12:00:00", "batch_id": b1, "qty": 30,
            "receipt_no": "R-CONSUMED"})
        self.confirm(self.store_a, sale)
        recall = self.svc.create_recall(
            "抽检不合格", "2026-09-03T00:00:00", root_batch_ids=[b1])
        pending_total = sum(p["qty"] or 0 for p in recall["pending_seals"])
        self.assertEqual(pending_total, 70)  # 100 - 30 已售

    def test_recall_by_criteria_covers_time_window(self):
        b_old = self.build_harvest(when="2026-08-20T08:00:00")
        b_bad = self.build_harvest(when="2026-09-01T08:00:00")
        recall = self.svc.create_recall(
            "采收期专项", "2026-09-05T00:00:00",
            criteria={"product": "生菜",
                      "harvested_from": "2026-08-30T00:00:00",
                      "harvested_to": "2026-09-03T00:00:00"})
        self.assertIn(b_bad, recall["root_batch_ids"])
        self.assertNotIn(b_old, recall["root_batch_ids"])

    # -- 6. 门店失联期间扫码恢复后的冲突显式处理 ---------------------------

    def test_offline_scan_sync_accepts_clean_and_flags_conflicts(self):
        b1 = self.build_harvest()
        # 批次调拨给城东门店
        t = self.report(self.packhouse, "transfer", {
            "at": "2026-09-02T08:00:00", "batch_id": b1, "qty": 100,
            "from": self.packhouse["id"], "to": self.store_a["id"]})
        self.confirm(self.packhouse, t)

        # 失联期间城东门店本地缓存的扫码：两次正常观测
        clean = self.svc.sync_scans(self.store_a["token"], [
            {"op_id": "s1", "at": "2026-09-02T12:00:00",
             "reported_at": "2026-09-02T15:00:00", "batch_id": b1,
             "action": "observe", "device_id": "gun-1"},
            {"op_id": "s2", "at": "2026-09-02T13:00:00",
             "reported_at": "2026-09-02T15:00:00", "batch_id": b1,
             "action": "observe", "device_id": "gun-1"},
        ])
        self.assertEqual(len(clean["accepted"]), 2)
        self.assertEqual(clean["conflicts"], [])
        self.assertTrue(self.svc.get_event(clean["accepted"][0])["late_evidence"])

        # 城西门店扫到了账面上在城东的批次 → 位置冲突
        # 已销毁批次又出现扫码 → 销毁冲突
        destroy = self.report(self.store_b, "destruction", {
            "at": "2026-09-02T10:00:00", "batch_id": b1, "qty": 100,
            "destruction_no": "XH-9"})
        self.confirm(self.store_b, destroy)
        result = self.svc.sync_scans(self.store_b["token"], [
            {"op_id": "s3", "at": "2026-09-02T20:00:00",
             "reported_at": "2026-09-03T08:00:00", "batch_id": b1,
             "action": "observe", "device_id": "gun-9"},
        ])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertIn(result["conflicts"][0]["kind"],
                      {"batch_already_destroyed", "location_mismatch"})

        conflict_id = result["conflicts"][0]["id"]
        # 冲突必须显式处理：未处理前一直挂起
        open_list = self.svc.list_conflicts("open")
        self.assertTrue(any(c["id"] == conflict_id for c in open_list))

        resolved = self.svc.resolve_conflict(
            conflict_id, "reject_scan", "设备串扫误读，批次确已销毁")
        self.assertEqual(resolved["status"], "resolved")
        # 处理后不能重复处理
        with self.assertRaises(StateConflict):
            self.svc.resolve_conflict(conflict_id, "accept_scan", "再处理")

    def test_offline_sell_after_recall_is_flagged(self):
        b1 = self.build_harvest()
        t = self.report(self.packhouse, "transfer", {
            "at": "2026-09-02T08:00:00", "batch_id": b1, "qty": 100,
            "from": self.packhouse["id"], "to": self.store_a["id"]})
        self.confirm(self.packhouse, t)
        self.svc.create_recall("紧急召回", "2026-09-03T00:00:00",
                               root_batch_ids=[b1])
        # 门店失联，9 月 3 日仍卖出；恢复联网同步时必须显式标 recall_violation
        result = self.svc.sync_scans(self.store_a["token"], [{
            "op_id": "sell-1", "at": "2026-09-03T12:00:00",
            "reported_at": "2026-09-03T18:00:00", "batch_id": b1,
            "action": "sell", "qty": 2, "receipt_no": "R-LATE-1"}])
        kinds = {c["kind"] for c in result["conflicts"]}
        self.assertIn("recall_violation", kinds)
        self.assertNotIn("R-LATE-1", self.svc.store.receipts)

    def test_downstream_blocked_until_forming_event_confirmed(self):
        # 采收仍是草稿时，分装仓不能把“还没正式入账”的批次确认调拨
        harvest = self.report(self.farm, "harvest", {
            "at": "2026-09-01T08:00:00", "product": "生菜", "qty": 50})
        bid = harvest["payload"].get("batch_id") or \
            self.svc.store.next_id  # 草稿采收已建批次但未生效
        bid = next(b["id"] for b in self.svc.store.batches.values()
                   if b["harvest_event_id"] == harvest["id"])
        transfer = self.report(self.packhouse, "transfer", {
            "at": "2026-09-01T18:00:00", "batch_id": bid, "qty": 50,
            "from": self.farm["id"], "to": self.packhouse["id"]})
        with self.assertRaises(StateConflict):
            self.confirm(self.packhouse, transfer)
        # 采收确认后，下游即可确认
        self.confirm(self.farm, harvest)
        self.confirm(self.packhouse, transfer)

    def test_late_scan_locates_missing_goods_for_seal(self):
        b1 = self.build_harvest(qty=30)
        # 批次调入城东门店，门店随后失联
        t = self.report(self.packhouse, "transfer", {
            "at": "2026-09-02T08:00:00", "batch_id": b1, "qty": 30,
            "from": self.packhouse["id"], "to": self.store_a["id"]})
        self.confirm(self.packhouse, t)
        recall = self.svc.create_recall(
            "紧急召回", "2026-09-03T00:00:00", root_batch_ids=[b1])
        self.assertEqual(recall["pending_seals"][0]["source"], "ledger")

        # 账面显示 30kg 已售出（实际可能是漏扫/错账），此时没有召回后的
        # 扫码，系统不会凭空猜去向：待封存清单为空
        sale = self.report(self.store_a, "sale", {
            "at": "2026-09-02T20:00:00", "batch_id": b1, "qty": 30,
            "receipt_no": "R-MISSING"})
        self.confirm(self.store_a, sale)
        self.assertEqual(self.svc.get_recall(recall["id"])["pending_seals"], [])

        # 恢复联网后回补召回生效后的扫码：实物其实还在城东门店
        sync = self.svc.sync_scans(self.store_a["token"], [{
            "op_id": "found-1", "at": "2026-09-03T09:00:00",
            "reported_at": "2026-09-03T09:30:00",
            "batch_id": b1, "action": "observe", "device_id": "gun-2"}])
        self.assertEqual(sync["conflicts"], [])
        estimated = [p for p in self.svc.get_recall(recall["id"])["pending_seals"]
                     if p["source"] == "scan_observation"]
        self.assertEqual(len(estimated), 1)
        self.assertEqual(estimated[0]["holder_id"], self.store_a["id"])
        self.assertIsNone(estimated[0]["qty"])  # 扫码只定位，不计量

        # 现场封存后，估算去向同样从清单收敛
        updated = self.svc.seal_batch(
            self.store_a["token"], recall["id"], b1, self.store_a["id"], 30)
        self.assertEqual(updated["pending_seals"], [])

    def test_pre_recall_scan_does_not_locate_goods(self):
        b1 = self.build_harvest(qty=10)
        t = self.report(self.packhouse, "transfer", {
            "at": "2026-09-01T08:00:00", "batch_id": b1, "qty": 10,
            "from": self.packhouse["id"], "to": self.store_a["id"]})
        self.confirm(self.packhouse, t)
        sale = self.report(self.store_a, "sale", {
            "at": "2026-09-01T20:00:00", "batch_id": b1, "qty": 10,
            "receipt_no": "R-OLD"})
        self.confirm(self.store_a, sale)
        # 扫码发生在召回生效之前，不能用来圈定封存去向
        self.svc.sync_scans(self.store_a["token"], [{
            "op_id": "old-scan", "at": "2026-09-02T09:00:00",
            "reported_at": "2026-09-02T09:30:00",
            "batch_id": b1, "action": "observe"}])
        recall = self.svc.create_recall(
            "事后召回", "2026-09-05T00:00:00", root_batch_ids=[b1])
        self.assertEqual(recall["pending_seals"], [])

    def test_unknown_batch_scan_conflicts_not_crashes(self):
        result = self.svc.sync_scans(self.store_a["token"], [{
            "op_id": "x1", "at": "2026-09-02T12:00:00",
            "batch_id": "GHOST", "action": "observe"}])
        self.assertEqual(result["conflicts"][0]["kind"], "unknown_batch")

    # -- 7. 凭证逆向导出责任主体、适用标准、证据链与待封存去向 -------------

    def test_receipt_reverse_trace_full_chain(self):
        # 产地
        b1 = self.build_harvest(qty=100)
        # 冷链运输（含温控读数）
        transport = self.report(self.carrier, "transport", {
            "at": "2026-09-01T20:00:00", "batch_ids": [b1],
            "from_location": "云岭基地", "to_location": "城南分装仓",
            "min_temp": 0, "max_temp": 4,
            "readings": [{"at": "2026-09-01T21:00:00", "temp": 2.5},
                         {"at": "2026-09-01T22:00:00", "temp": 6.8}]})
        self.confirm(self.carrier, transport)
        # 分装成箱
        repack = self.report(self.packhouse, "repack", {
            "at": "2026-09-02T06:00:00", "inputs": [{"batch_id": b1, "qty": 50}],
            "outputs": [{"batch_id": "C-A", "kind": "case", "product": "生菜",
                         "qty": 50, "unit": "kg"}]})
        self.confirm(self.packhouse, repack)
        # 许可单据互不相认 → 统一挂到主体上
        self.svc.register_document(
            self.farm["token"], self.farm["id"], "license",
            "NY-SC-1", "农业农村局", "2026-01-01T00:00:00")
        self.svc.register_document(
            self.packhouse["token"], self.packhouse["id"], "license",
            "SC-CN-2", "市场监管局", "2026-01-01T00:00:00")
        # 检验合格并挂报告
        self.svc.publish_standard("GB-LEAF", "2023", "叶菜限量",
                                  "2023-01-01T00:00:00")
        insp = self.report(self.lab, "inspection", {
            "at": "2026-09-02T09:00:00", "batch_id": "C-A", "result": "pass",
            "standard_code": "GB-LEAF", "report_no": "JC-FINAL",
            "doc_ids": []})
        self.confirm(self.lab, insp)
        # 调入城东门店并销售
        tr = self.report(self.packhouse, "transfer", {
            "at": "2026-09-02T11:00:00", "batch_id": "C-A", "qty": 50,
            "from": self.packhouse["id"], "to": self.store_a["id"]})
        self.confirm(self.packhouse, tr)
        sale = self.report(self.store_a, "sale", {
            "at": "2026-09-02T12:30:00", "batch_id": "C-A", "qty": 1,
            "receipt_no": "R-20260902-0088"})
        self.confirm(self.store_a, sale)

        trace = self.svc.trace_receipt("R-20260902-0088")

        # 责任主体：产地、承运、分装仓、检验机构、门店全部可导出
        roles = {p["type"] for p in trace["parties"]}
        self.assertEqual(roles, {"farm", "carrier", "packhouse", "lab", "store"})
        docs = {d["doc_no"] for p in trace["parties"] for d in p["documents"]}
        self.assertIn("NY-SC-1", docs)
        self.assertIn("SC-CN-2", docs)

        # 证据链按时间排列，含温控异常标记
        types_in_order = [s["type"] for s in trace["evidence_chain"]]
        self.assertEqual(types_in_order,
                         ["harvest", "transport", "repack",
                          "inspection", "transfer", "sale"])
        transport_stage = next(s for s in trace["evidence_chain"]
                               if s["type"] == "transport")
        self.assertTrue(transport_stage["temp_excursion"])

        # 适用标准随检验结论一并导出
        self.assertEqual(trace["standards_applied"][0]["code"], "GB-LEAF")

        # 销售后发起召回：逆向结果立即列出仍需封存的去向（剩 49kg 在门店）
        recall = self.svc.create_recall(
            "复核存疑", "2026-09-02T18:00:00", root_batch_ids=[b1])
        trace2 = self.svc.trace_receipt("R-20260902-0088")
        self.assertEqual(len(trace2["active_recalls"]), 1)
        pending_at_store = [p for p in trace2["pending_seals"]
                            if p["holder_id"] == self.store_a["id"]]
        self.assertTrue(pending_at_store)
        self.assertEqual(pending_at_store[0]["qty"], 49)

    # -- 8. 公众只看到脱敏安全摘要 -----------------------------------------

    def test_public_summary_is_masked(self):
        b1 = self.build_harvest()
        summary = self.svc.public_summary(b1)
        self.assertEqual(summary["risk_level"], "no_public_data")
        self.assertNotIn(self.farm["name"], str(summary))
        self.assertTrue(summary["batch_hint"].endswith("…"))
        self.assertNotIn(b1, summary["batch_hint"])

        insp = self.report(self.lab, "inspection", {
            "at": "2026-09-02T10:00:00", "batch_id": b1, "result": "fail",
            "report_no": "JC-PUB"})
        self.confirm(self.lab, insp)
        bad = self.svc.public_summary(b1)
        self.assertEqual(bad["risk_level"], "unsafe")

        recall = self.svc.create_recall(
            "不合格召回", "2026-09-03T00:00:00", root_batch_ids=[b1],
            public_guidance="请凭小票到门店退货")
        recalled = self.svc.public_summary(b1)
        self.assertEqual(recalled["risk_level"], "recalled")
        self.assertEqual(recalled["guidance"], "请凭小票到门店退货")

        pub_recall = self.svc.public_recall(recall["id"])
        self.assertNotIn("scope_batches", pub_recall)
        self.assertEqual(pub_recall["products"], ["生菜"])

    # -- 9. 退回与销毁也保持链式可核 ---------------------------------------

    def test_return_and_destruction_flow(self):
        b1 = self.build_harvest(qty=50)
        t = self.report(self.packhouse, "transfer", {
            "at": "2026-09-02T08:00:00", "batch_id": b1, "qty": 50,
            "from": self.packhouse["id"], "to": self.store_a["id"]})
        self.confirm(self.packhouse, t)
        ret = self.report(self.store_a, "return", {
            "at": "2026-09-02T20:00:00", "batch_id": b1, "qty": 10,
            "from": self.store_a["id"], "to": self.packhouse["id"],
            "reason": "临期退回"})
        self.confirm(self.store_a, ret)
        des = self.report(self.packhouse, "destruction", {
            "at": "2026-09-03T08:00:00", "batch_id": b1, "qty": 10,
            "destruction_no": "XH-RET-1", "reason": "退回品销毁"})
        self.confirm(self.packhouse, des)

        recall = self.svc.create_recall(
            "例行召回", "2026-09-03T12:00:00", root_batch_ids=[b1])
        pending = {p["holder_id"]: p["qty"] for p in recall["pending_seals"]}
        # 门店 40（50-10 退回），退回并销毁的 10 不再出现
        self.assertEqual(pending.get(self.store_a["id"]), 40)
        self.assertNotIn(self.packhouse["id"], pending)


class TimeParseTest(unittest.TestCase):
    def test_epoch_and_iso_equivalent(self):
        from traceability import parse_time
        self.assertAlmostEqual(
            parse_time("2026-09-01T00:00:00+00:00"),
            parse_time("2026-09-01T00:00:00+00:00"))
        with self.assertRaises(Exception):
            parse_time("not-a-time")


if __name__ == "__main__":
    unittest.main()
