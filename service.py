"""食品全链条追溯服务运行入口。

保留健康检查，并暴露追溯领域的 JSON 接口：

* 企业侧：上报事件（采收/运输/分装/检验/销售/退回/销毁）、维护草稿、
  确认记录、离线扫码回补。
* 执法侧：裁定口径分歧、发起召回与封存、结案、按消费凭证逆向导出证据链。
* 公众侧：只返回脱敏安全摘要。
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from traceability import Forbidden, TraceError, TraceService, jsonable

SERVICE_ID = "food-traceability"
SERVICE_NAME = "食品全链条追溯"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 进程级单例；测试可通过 Handler.reset_service() 重建
SERVICE = TraceService()


class Handler(BaseHTTPRequestHandler):
    """HTTP 适配层：鉴权、路由与 JSON 编解码，逻辑全部在 TraceService。"""

    service = SERVICE

    @classmethod
    def reset_service(cls):
        cls.service = TraceService()
        return cls.service

    # -- 基础工具 ----------------------------------------------------------

    def _send_json(self, payload, status=200):
        body = json.dumps(jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise TraceError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise TraceError("请求体必须是 JSON 对象")
        return data

    def _token(self) -> str | None:
        return self.headers.get("X-Token")

    def _auth(self) -> dict:
        return self.service._authenticate(self._token())  # noqa: SLF001

    def _auth_authority(self) -> dict:
        party = self._auth()
        if party["type"] != "authority":
            raise Forbidden("该接口仅对执法部门开放")
        return party

    # -- 路由 --------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            self._route(method, path, query)
        except TraceError as error:
            self._send_json({"error": str(error)}, status=error.status)
        except Exception as error:  # 防御性兜底，避免连接被挂起
            self._send_json({"error": f"服务器内部错误：{error}"}, status=500)

    def _route(self, method, path, query):
        if method == "GET" and path == "/health":
            self._send_json(health_payload())
            return

        # 公众侧（无需令牌，仅脱敏信息）
        match = re.fullmatch(r"/public/batches/([^/]+)", path)
        if method == "GET" and match:
            self._send_json(self.service.public_summary(batch_id=match.group(1)))
            return
        match = re.fullmatch(r"/public/receipts/([^/]+)", path)
        if method == "GET" and match:
            self._send_json(self.service.public_summary(receipt_no=match.group(1)))
            return
        match = re.fullmatch(r"/public/recalls/([^/]+)", path)
        if method == "GET" and match:
            self._send_json(self.service.public_recall(match.group(1)))
            return

        if method == "POST" and path == "/parties":
            body = self._read_json()
            self._send_json(self.service.register_party(
                body["name"], body["type"], body.get("contact")), status=201)
            return

        match = re.fullmatch(r"/parties/([^/]+)/documents", path)
        if method == "POST" and match:
            body = self._read_json()
            caller = self.service._authenticate(self._token())  # noqa: SLF001
            is_regulator = caller["type"] == "authority"
            self._send_json(self.service.register_document(
                self._token(), match.group(1), body["doc_type"], body["doc_no"],
                body["issuer"], body["issued_at"], body.get("expires_at"),
                is_regulator=is_regulator), status=201)
            return

        if method == "POST" and path == "/standards":
            self._auth_authority()
            body = self._read_json()
            self._send_json(self.service.publish_standard(
                body["code"], body["version"], body["title"],
                body["effective_at"], body.get("expires_at")), status=201)
            return

        if method == "POST" and path == "/events":
            event_type = query.get("type", [None])[0]
            body = self._read_json()
            self._send_json(self.service.report_event(
                self._token(), event_type, body,
                self.headers.get("X-Op-Id")), status=201)
            return

        match = re.fullmatch(r"/events/([^/]+)", path)
        if method == "GET" and match:
            self._auth()
            self._send_json(self.service.get_event(match.group(1)))
            return

        match = re.fullmatch(r"/events/([^/]+)/edit", path)
        if method == "POST" and match:
            body = self._read_json()
            self._send_json(self.service.edit_draft(
                self._token(), match.group(1), body))
            return

        match = re.fullmatch(r"/events/([^/]+)/confirm", path)
        if method == "POST" and match:
            self._send_json(self.service.confirm_event(
                self._token(), match.group(1)))
            return

        match = re.fullmatch(r"/events/([^/]+)/resolve-dispute", path)
        if method == "POST" and match:
            self._auth_authority()
            body = self._read_json()
            self._send_json(self.service.resolve_dispute(
                match.group(1), body["chosen_party_id"], body["reason"]))
            return

        if method == "POST" and path == "/scans/sync":
            body = self._read_json()
            self._send_json(self.service.sync_scans(
                self._token(), body.get("scans", [])))
            return

        if method == "GET" and path == "/conflicts":
            self._auth_authority()
            status = query.get("status", [None])[0]
            self._send_json({"conflicts": self.service.list_conflicts(status)})
            return

        match = re.fullmatch(r"/conflicts/([^/]+)/resolve", path)
        if method == "POST" and match:
            self._auth_authority()
            body = self._read_json()
            self._send_json(self.service.resolve_conflict(
                match.group(1), body["decision"], body["reason"]))
            return

        match = re.fullmatch(r"/batches/([^/]+)/lineage", path)
        if method == "GET" and match:
            self._auth()
            direction = query.get("direction", ["both"])[0]
            self._send_json(self.service.lineage(match.group(1), direction))
            return

        if method == "POST" and path == "/recalls":
            self._auth_authority()
            body = self._read_json()
            self._send_json(self.service.create_recall(
                body["reason"], body["effective_at"],
                body.get("root_batch_ids"), body.get("criteria"),
                body.get("public_guidance")), status=201)
            return

        match = re.fullmatch(r"/recalls/([^/]+)", path)
        if method == "GET" and match:
            self._auth_authority()
            self._send_json(self.service.get_recall(match.group(1)))
            return

        match = re.fullmatch(r"/recalls/([^/]+)/seals", path)
        if method == "POST" and match:
            body = self._read_json()
            self._auth()
            self._send_json(self.service.seal_batch(
                self._token(), match.group(1), body["batch_id"],
                body["holder_id"], body["qty"]))
            return

        if method == "POST" and path == "/cases":
            self._auth_authority()
            body = self._read_json()
            self._send_json(self.service.close_case(
                body["title"], body["inspection_event_ids"],
                body["judgment"]), status=201)
            return

        match = re.fullmatch(r"/cases/([^/]+)", path)
        if method == "GET" and match:
            self._auth_authority()
            self._send_json(self.service.get_case(match.group(1)))
            return

        match = re.fullmatch(r"/trace/receipts/([^/]+)", path)
        if method == "GET" and match:
            self._auth_authority()
            self._send_json(self.service.trace_receipt(match.group(1)))
            return

        self._send_json({"error": "接口不存在"}, status=404)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 领域核心冒烟：保证基础规则可执行
        svc = TraceService()
        farm = svc.register_party("自检产地", "farm")
        event = svc.report_event(farm["token"], "harvest", {
            "at": "2026-09-01T08:00:00", "product": "生菜", "qty": 100,
            "unit": "kg", "origin": "云南某基地"})
        svc.confirm_event(farm["token"], event["id"])
        batch_id = next(
            b["id"] for b in svc.store.batches.values()
            if b["harvest_event_id"] == event["id"])
        assert svc.public_summary(batch_id) is not None
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
