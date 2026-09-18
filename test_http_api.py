"""HTTP API 契约：角色鉴权、去重响应码、端到端执法链路。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import build_handler
from traceability import TraceabilityService


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.service = TraceabilityService()
        handler = build_handler(self.service)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None, role=None, party=None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if role:
            headers["X-Role"] = role
        # HTTP 头只能承载 latin-1：机构代码可走 X-Party，中文主体名走 body.party
        if party:
            try:
                party.encode("latin-1")
                headers["X-Party"] = party
            except UnicodeEncodeError:
                body = dict(body or {})
                body.setdefault("party", party)
                party = None
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def get(self, path, **kwargs):
        return self.call("GET", path, **kwargs)

    def post(self, path, body, **kwargs):
        return self.call("POST", path, body, **kwargs)


class IdentityTest(ApiTestBase):
    def test_health_keeps_contract(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "service": "food-traceability", "name": "食品全链条追溯"})

    def test_unknown_route_404(self):
        status, _ = self.get("/unknown")
        self.assertEqual(status, 404)

    def test_write_requires_role(self):
        status, body = self.post("/events", {"type": "harvest", "payload": {"lot": "X"}})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")


class ChainApiTest(ApiTestBase):
    HARVEST = {"lot": "B1", "product": "冻虾", "quantity": 100, "unit": "kg",
               "packaging": "bulk", "farm": "海湾养殖场", "producer": "海湾水产合作社"}

    def _harvest(self, role="agriculture", party="农业局", payload=None):
        return self.post(
            "/events",
            {"type": "harvest", "payload": payload or self.HARVEST,
             "occurred_at": "2026-08-10T06:00:00+00:00"},
            role=role, party=party,
        )

    def test_cross_party_duplicate_single_record(self):
        status1, first = self._harvest()
        self.assertEqual(status1, 201)
        self.assertFalse(first["deduped"])
        event_id = first["event"]["event_id"]

        # 平台、市场监管重复上报 -> 200 合并，不产生新事件
        for role, party in (("platform", "外卖平台"), ("market", "市场监管局"), ("health", "卫健委")):
            status, body = self._harvest(role=role, party=party)
            self.assertEqual(status, 200)
            self.assertTrue(body["deduped"])
            self.assertEqual(body["event"]["event_id"], event_id)

        status, events = self.get("/events?lot=B1")
        self.assertEqual(status, 200)
        self.assertEqual(len(events), 1)
        self.assertEqual({r["party"] for r in events[0]["reporters"]},
                         {"农业局", "外卖平台", "市场监管局", "卫健委"})

    def test_full_investigation_flow_over_http(self):
        self._harvest()
        # 冷链温控
        status, _ = self.post(
            "/events",
            {"type": "temperature",
             "payload": {"lot": "B1", "vehicle": "冷A-001", "temperature": -18.0,
                         "condition": "ok", "destination": "仓W1", "carrier": "某冷链公司"},
             "occurred_at": "2026-08-10T10:00:00+00:00"},
            role="enterprise", party="某冷链公司",
        )
        self.assertEqual(status, 201)
        # 分装成两箱
        status, repack = self.post(
            "/repack",
            {"inputs": [{"lot": "B1", "consumed": 100}],
             "outputs": [{"lot": "C1", "quantity": 50, "packaging": "case"},
                         {"lot": "C2", "quantity": 50, "packaging": "case"}],
             "location": "分装仓W1", "occurred_at": "2026-08-11T08:00:00+00:00"},
            role="enterprise", party="某食品公司",
        )
        self.assertEqual(status, 201)
        # 谱系：C1 完全来源于 B1
        status, origin = self.get("/lots/C1/origin")
        self.assertEqual(status, 200)
        self.assertEqual(origin["roots"][0]["lot"], "B1")
        self.assertAlmostEqual(origin["roots"][0]["share"], 1.0)
        # 标准 + 检验 + 确认
        self.post("/standards",
                  {"standard_id": "STD-1", "title": "水产限量", "limits": {"重金属": 0.5},
                   "effective_at": "2026-01-01T00:00:00+00:00"},
                  role="market", party="市场监管局")
        status, insp = self.post(
            "/events",
            {"type": "inspection",
             "payload": {"lot": "C1", "lab": "检测中心", "readings": {"重金属": 0.2}},
             "occurred_at": "2026-08-11T09:00:00+00:00"},
            role="market", party="市场监管局",
        )
        self.assertEqual(status, 201)
        status, _ = self.post(f"/events/{insp['event']['event_id']}/confirm", {},
                              role="market", party="市场监管局")
        self.assertEqual(status, 200)
        # 销售 + 消费凭证
        self.post("/events",
                  {"type": "sale", "payload": {"lot": "C1", "location": "门店S1", "buyer": "门店S1"},
                   "occurred_at": "2026-08-12T08:00:00+00:00"},
                  role="platform", party="外卖平台")
        status, _ = self.post(
            "/receipts",
            {"receipt": "RCP-1", "lots": ["C1"], "store": "门店S1",
             "sold_at": "2026-08-12T12:00:00+00:00"},
            role="platform", party="外卖平台",
        )
        self.assertEqual(status, 201)

        # 公众只能看到脱敏摘要
        status, summary = self.get("/public/lots/C1/summary")
        self.assertEqual(status, 200)
        self.assertNotIn("C1", json.dumps(summary, ensure_ascii=False))
        self.assertNotIn("某食品公司", json.dumps(summary, ensure_ascii=False))

        # 公众不能发起执法调查
        status, _ = self.get("/receipts/RCP-1/investigation")
        self.assertEqual(status, 403)
        # 执法人员：逆向证据链 + 封存清单
        status, report = self.get("/receipts/RCP-1/investigation",
                                  role="inspector", party="执法支队")
        self.assertEqual(status, 200)
        self.assertIn("海湾水产合作社", report["responsible_parties"])
        self.assertIn("某冷链公司", report["responsible_parties"])
        self.assertEqual(report["applicable_standards"][0]["standard_id"], "STD-1")
        chain = [e["type"] for e in report["evidence_chain"]]
        self.assertEqual(chain[0], "harvest")
        self.assertEqual(chain[-1], "sale")
        self.assertIn("C1", {row["lot"] for row in report["still_to_seize"]})

        # 封存后清单清空
        status, _ = self.post("/seizures",
                              {"lot": "C1", "reason": "调查封存", "case_id": "CASE-1",
                               "occurred_at": "2026-08-12T13:00:00+00:00"},
                              role="inspector", party="执法支队")
        self.assertEqual(status, 201)
        status, seized = self.get("/seizure-list?lots=C1")
        self.assertEqual(status, 200)
        self.assertEqual(seized, [])

    def test_conflict_returns_409_and_blocks_confirm(self):
        self._harvest()
        _, good = self.post(
            "/events",
            {"type": "inspection",
             "payload": {"lot": "B1", "lab": "检测中心", "readings": {"农残": 0.01}, "result": "pass"},
             "occurred_at": "2026-08-11T09:00:00+00:00"},
            role="market", party="市场监管局",
        )
        self.post(
            "/events",
            {"type": "inspection",
             "payload": {"lot": "B1", "lab": "复检实验室", "readings": {"农残": 0.9}, "result": "fail"},
             "occurred_at": "2026-08-11T10:00:00+00:00"},
            role="health", party="卫健委",
        )
        status, body = self.post(f"/events/{good['event']['event_id']}/confirm", {},
                                 role="market", party="市场监管局")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict")
        # 未决冲突列表可见
        status, pending = self.get("/conflicts")
        self.assertEqual(status, 200)
        self.assertEqual(len(pending), 2)

    def test_draft_revision_rules(self):
        _, harvest = self._harvest(role="enterprise", party="某食品公司")
        event_id = harvest["event"]["event_id"]
        # 别的企业不能改
        status, _ = self.post(f"/events/{event_id}/revise",
                              {"payload": {**self.HARVEST, "quantity": 9}},
                              role="enterprise", party="其他公司")
        self.assertEqual(status, 403)
        # 本企业可以改草稿
        status, revised = self.post(f"/events/{event_id}/revise",
                                    {"payload": {**self.HARVEST, "quantity": 9}},
                                    role="enterprise", party="某食品公司")
        self.assertEqual(status, 200)
        self.assertEqual(revised["payload"]["quantity"], 9)
        # 确认后再改被拒
        self.post(f"/events/{event_id}/confirm", {}, role="enterprise", party="某食品公司")
        status, body = self.post(f"/events/{event_id}/revise",
                                 {"payload": {**self.HARVEST, "quantity": 8}},
                                 role="enterprise", party="某食品公司")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
