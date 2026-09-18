"""HTTP 契约测试：健康检查保持稳定，追溯接口按角色暴露正确能力。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, health_payload


class HttpTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.reset_service()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    @classmethod
    def request(cls, method, path, body=None, token=None, op_id=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Token"] = token
        if op_id:
            headers["X-Op-Id"] = op_id
        req = Request(f"{cls.base_url}{path}", data=data, headers=headers,
                      method=method)
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.load(error)
            error.close()
            return error.code, payload

    @classmethod
    def register(cls, name, party_type):
        status, body = cls.request("POST", "/parties",
                                   {"name": name, "type": party_type})
        assert status == 201, body
        return body

    @classmethod
    def report(cls, token, event_type, body, op_id=None):
        return cls.request("POST", f"/events?type={event_type}", body,
                           token=token, op_id=op_id)


class HealthContractTest(HttpTestBase):
    def test_health_payload_has_stable_identity(self):
        self.assertEqual(health_payload(), {"status": "ok", "service": SERVICE_ID,
                                            "name": "食品全链条追溯"})

    def test_health_endpoint_returns_json(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_unknown_route_is_not_exposed(self):
        status, body = self.request("GET", "/unknown")
        self.assertEqual(status, 404)


class RoleAndTraceApiTest(HttpTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.farm = cls.register("云岭菜园", "farm")
        cls.packhouse = cls.register("城南分装仓", "packhouse")
        cls.lab = cls.register("市检验中心", "lab")
        cls.store = cls.register("城东门店", "store")
        cls.gov = cls.register("市场监管局", "authority")

    def test_01_requires_token(self):
        status, body = self.request("POST", "/events?type=harvest", {})
        self.assertEqual(status, 403)

    def test_02_enterprise_chain_over_http(self):
        # 采收 → 确认 → 分装 → 调拨 → 销售
        status, harvest = self.report(self.farm["token"], "harvest", {
            "at": "2026-09-01T08:00:00", "product": "生菜", "qty": 100,
            "unit": "kg"})
        self.assertEqual(status, 201)
        self.assertEqual(harvest["status"], "draft")
        status, confirmed = self.request(
            "POST", f"/events/{harvest['id']}/confirm", token=self.farm["token"])
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        b1 = confirmed["payload"]["batch_id"]

        # 确认后不可改
        status, body = self.request(
            "POST", f"/events/{harvest['id']}/edit", {"qty": 1},
            token=self.farm["token"])
        self.assertEqual(status, 409)

        status, repack = self.report(self.packhouse["token"], "repack", {
            "at": "2026-09-02T06:00:00",
            "inputs": [{"batch_id": b1, "qty": 100}],
            "outputs": [{"batch_id": "C1", "kind": "case", "product": "生菜",
                         "qty": 100, "unit": "kg"}]})
        self.assertEqual(status, 201, repack)
        self.request("POST", f"/events/{repack['id']}/confirm",
                     token=self.packhouse["token"])

        status, transfer = self.report(self.packhouse["token"], "transfer", {
            "at": "2026-09-02T08:00:00", "batch_id": "C1", "qty": 100,
            "from": self.packhouse["id"], "to": self.store["id"]})
        self.request("POST", f"/events/{transfer['id']}/confirm",
                     token=self.packhouse["token"])

        status, sale = self.report(self.store["token"], "sale", {
            "at": "2026-09-02T12:00:00", "batch_id": "C1", "qty": 2,
            "receipt_no": "R-HTTP-1"})
        self.assertEqual(status, 201)
        self.request("POST", f"/events/{sale['id']}/confirm",
                     token=self.store["token"])
        self.__class__.batch_id = b1

    def test_03_idempotent_retry(self):
        status, first = self.report(self.store["token"], "sale", {
            "at": "2026-09-02T13:00:00", "batch_id": "C1", "qty": 1,
            "receipt_no": "R-HTTP-2"}, op_id="client-op-1")
        status, second = self.report(self.store["token"], "sale", {
            "at": "2026-09-02T13:00:00", "batch_id": "C1", "qty": 1,
            "receipt_no": "R-HTTP-2"}, op_id="client-op-1")
        self.assertEqual(first["id"], second["id"])

    def test_04_standard_and_inspection(self):
        status, std = self.request("POST", "/standards", {
            "code": "GB-LEAF", "version": "2023", "title": "叶菜限量",
            "effective_at": "2023-01-01T00:00:00"}, token=self.gov["token"])
        self.assertEqual(status, 201)
        # 企业不能发布标准
        status, body = self.request("POST", "/standards", {
            "code": "X", "version": "1", "title": "x",
            "effective_at": "2023-01-01T00:00:00"}, token=self.lab["token"])
        self.assertEqual(status, 403)

        status, insp = self.report(self.lab["token"], "inspection", {
            "at": "2026-09-02T09:00:00", "batch_id": "C1", "result": "pass",
            "standard_id": std["id"], "report_no": "JC-HTTP-1"})
        status, insp = self.request(
            "POST", f"/events/{insp['id']}/confirm", token=self.lab["token"])
        self.assertEqual(status, 200)
        self.assertEqual(insp["standard_snapshot"]["version"], "2023")

    def test_05_public_summary_is_anonymous_and_masked(self):
        status, body = self.request("GET", "/public/batches/C1")
        self.assertEqual(status, 200)
        self.assertNotIn("parties", body)
        self.assertTrue(body["batch_hint"].endswith("…"))
        self.assertNotIn("C1", body["batch_hint"])

        status, by_receipt = self.request(
            "GET", "/public/receipts/R-HTTP-1")
        self.assertEqual(status, 200)
        self.assertEqual(by_receipt["risk_level"], "tested_pass")

    def test_06_lineage_requires_any_party(self):
        status, body = self.request("GET", "/batches/C1/lineage")
        self.assertEqual(status, 403)
        status, body = self.request(
            "GET", "/batches/C1/lineage?direction=up", token=self.store["token"])
        self.assertEqual(status, 200)
        self.assertTrue(any(n["batch_id"] == self.batch_id
                            for n in body["upstream"]))

    def test_07_recall_seal_and_authority_trace(self):
        # 召回仅执法可发起
        status, body = self.request("POST", "/recalls", {
            "reason": "农残超标", "effective_at": "2026-09-03T00:00:00",
            "root_batch_ids": [self.batch_id]}, token=self.store["token"])
        self.assertEqual(status, 403)

        status, recall = self.request("POST", "/recalls", {
            "reason": "农残超标", "effective_at": "2026-09-03T00:00:00",
            "root_batch_ids": [self.batch_id]}, token=self.gov["token"])
        self.assertEqual(status, 201)
        self.assertIn("C1", recall["scope_batches"])
        pending_holders = {p["holder_id"] for p in recall["pending_seals"]}
        self.assertIn(self.store["id"], pending_holders)

        # 门店封存自己名下的数量
        store_qty = next(p["qty"] for p in recall["pending_seals"]
                         if p["holder_id"] == self.store["id"])
        status, updated = self.request(
            "POST", f"/recalls/{recall['id']}/seals",
            {"batch_id": "C1", "holder_id": self.store["id"],
             "qty": store_qty}, token=self.store["token"])
        self.assertEqual(status, 200)
        self.assertNotIn(self.store["id"],
                         {p["holder_id"] for p in updated["pending_seals"]})

        # 执法方按消费凭证逆向导出
        status, trace = self.request(
            "GET", "/trace/receipts/R-HTTP-1", token=self.gov["token"])
        self.assertEqual(status, 200)
        self.assertEqual({p["type"] for p in trace["parties"]},
                         {"farm", "packhouse", "lab", "store"})
        self.assertEqual(trace["standards_applied"][0]["code"], "GB-LEAF")
        self.assertTrue(any(s["type"] == "sale"
                            for s in trace["evidence_chain"]))

        # 公众不能访问执法追溯
        status, body = self.request("GET", "/trace/receipts/R-HTTP-1")
        self.assertEqual(status, 403)

        # 公众召回通告只暴露安全信息
        status, pub = self.request(
            "GET", f"/public/recalls/{recall['id']}")
        self.assertEqual(status, 200)
        self.assertNotIn("scope_batches", pub)
        self.assertEqual(pub["scope_batch_count"], 2)

    def test_08_offline_scan_conflict_lifecycle(self):
        # 新采收一批，调入门店后整批销毁，再回补“又出现”的离线扫码
        status, harvest = self.report(self.farm["token"], "harvest", {
            "at": "2026-09-04T08:00:00", "product": "菠菜", "qty": 10,
            "unit": "kg"})
        self.request("POST", f"/events/{harvest['id']}/confirm",
                     token=self.farm["token"])
        b2 = harvest["payload"]["batch_id"]
        status, destroy = self.report(self.store["token"], "destruction", {
            "at": "2026-09-04T18:00:00", "batch_id": b2, "qty": 10,
            "destruction_no": "XH-HTTP-1"})
        self.request("POST", f"/events/{destroy['id']}/confirm",
                     token=self.store["token"])

        status, sync = self.request("POST", "/scans/sync", {
            "scans": [{"op_id": "offline-1", "at": "2026-09-04T20:00:00",
                       "reported_at": "2026-09-05T08:00:00",
                       "batch_id": b2, "action": "observe"}]},
            token=self.store["token"])
        self.assertEqual(status, 200)
        self.assertEqual(len(sync["conflicts"]), 1)
        conflict_id = sync["conflicts"][0]["id"]

        # 只有执法方能列出和处理冲突
        status, body = self.request("GET", "/conflicts?status=open",
                                    token=self.store["token"])
        self.assertEqual(status, 403)
        status, open_list = self.request("GET", "/conflicts?status=open",
                                         token=self.gov["token"])
        self.assertEqual(status, 200)
        self.assertTrue(any(c["id"] == conflict_id
                            for c in open_list["conflicts"]))

        status, resolved = self.request(
            "POST", f"/conflicts/{conflict_id}/resolve",
            {"decision": "reject_scan", "reason": "设备误扫"},
            token=self.gov["token"])
        self.assertEqual(status, 200)
        self.assertEqual(resolved["status"], "resolved")

    def test_09_invalid_json_is_400(self):
        req = Request(f"{self.base_url}/parties", data=b"{not-json",
                      headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=3)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
