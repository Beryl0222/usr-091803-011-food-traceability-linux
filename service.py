"""食品全链条追溯服务运行入口。

在原有健康检查之上提供追溯 JSON API。调用方通过请求头表明身份：

* ``X-Role``：enterprise / agriculture / market / health / platform / inspector
* ``X-Party``：责任主体名称（企业、部门或机构）

公众接口不需要身份头。所有写操作都是对只增账本的追加；草稿可由企业修订，
确认后冻结，冲突与结案规则由 ``traceability`` 领域层强制。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from traceability import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    TraceabilityError,
    TraceabilityService,
    ValidationError,
)

SERVICE_ID = "food-traceability"
SERVICE_NAME = "食品全链条追溯"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_handler(service):
    """构造绑定到指定领域服务实例的请求处理器（便于测试注入）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "FoodTrace/1.0"

        # ------------------------------------------------------------
        # 基础收发
        # ------------------------------------------------------------

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValidationError("请求体必须是合法的 UTF-8 JSON")
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def _reporter(self, body):
            return {"role": self.headers.get("X-Role"), "party": self.headers.get("X-Party") or body.get("party")}

        def _require_reporter(self, body):
            reporter = self._reporter(body)
            if not reporter["role"]:
                raise AuthorizationError("缺少 X-Role 身份头")
            return reporter

        def _handle(self, handler):
            try:
                handler()
            except ValidationError as exc:
                self._send_json(400, {"error": "validation_error", "message": str(exc)})
            except AuthorizationError as exc:
                self._send_json(403, {"error": "forbidden", "message": str(exc)})
            except NotFoundError as exc:
                self._send_json(404, {"error": "not_found", "message": str(exc)})
            except ConflictError as exc:
                self._send_json(409, {"error": "conflict", "message": str(exc)})
            except TraceabilityError as exc:
                self._send_json(422, {"error": "domain_error", "message": str(exc)})

        def log_message(self, *_args):
            return

        # ------------------------------------------------------------
        # 路由
        # ------------------------------------------------------------

        def do_GET(self):
            self._handle(self._route_get)

        def do_POST(self):
            self._handle(self._route_post)

        def _route_get(self):
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            query = parse_qs(urlsplit(self.path).query)
            if parts == ["health"]:
                self._send_json(200, health_payload())
            elif len(parts) == 2 and parts[0] == "events":
                self._send_json(200, service.get_event(parts[1]))
            elif parts == ["events"]:
                self._send_json(200, service.list_events(
                    event_type=query.get("type", [None])[0],
                    lot_id=query.get("lot", [None])[0],
                ))
            elif len(parts) == 3 and parts[0] == "lots" and parts[2] == "origin":
                self._send_json(200, service.trace_origin(parts[1]))
            elif len(parts) == 3 and parts[0] == "lots" and parts[2] == "destinations":
                self._send_json(200, service.trace_destinations(parts[1]))
            elif len(parts) == 4 and parts[0] == "public" and parts[1] == "lots" and parts[3] == "summary":
                self._send_json(200, service.public_summary(parts[2]))
            elif len(parts) == 2 and parts[0] == "recalls":
                self._send_json(200, service.get_recall(parts[1]))
            elif len(parts) == 2 and parts[0] == "cases":
                self._send_json(200, service.get_case(parts[1]))
            elif parts == ["conflicts"]:
                self._send_json(200, service.pending_conflicts())
            elif parts == ["seizure-list"]:
                lots = query.get("lots", [""])[0].split(",")
                self._send_json(200, service.still_to_seize([x for x in lots if x]))
            elif len(parts) == 3 and parts[0] == "receipts" and parts[2] == "investigation":
                reporter = self._require_reporter({})
                self._send_json(200, service.investigate_receipt(parts[1], reporter))
            else:
                self._send_json(404, {"error": "not_found", "message": "未知路由"})

        def _route_post(self):
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            body = self._read_body()

            if parts == ["lots"]:
                reporter = self._require_reporter(body)
                result = service.register_lot(
                    packaging=body["packaging"], product=body["product"],
                    quantity=body["quantity"], unit=body["unit"],
                    owner=body.get("owner") or reporter["party"],
                    lot_id=body.get("lot"), extra=body.get("extra"),
                    occurred_at=body.get("occurred_at"),
                )
                self._send_json(201, result)

            elif parts == ["events"]:
                reporter = self._require_reporter(body)
                event, deduped = service.submit_event(
                    body["type"], reporter, body.get("payload", body.get("event", body)),
                    occurred_at=body.get("occurred_at"), reporter_ref=body.get("reporter_ref"),
                )
                self._send_json(200 if deduped else 201,
                                {"deduped": deduped, "event": _public_event(event)})

            elif parts == ["repack"]:
                reporter = self._require_reporter(body)
                event = service.repack(
                    reporter, inputs=body["inputs"], outputs=body["outputs"],
                    location=body.get("location"), occurred_at=body.get("occurred_at"),
                    notes=body.get("notes"),
                )
                self._send_json(201, _public_event(event))

            elif len(parts) == 3 and parts[0] == "events" and parts[2] == "revise":
                reporter = self._require_reporter(body)
                event = service.revise_event(parts[1], reporter, body["payload"])
                self._send_json(200, _public_event(event))

            elif len(parts) == 3 and parts[0] == "events" and parts[2] == "confirm":
                reporter = self._require_reporter(body)
                event = service.confirm_event(parts[1], reporter)
                self._send_json(200, _public_event(event))

            elif len(parts) == 3 and parts[0] == "events" and parts[2] == "resolve-conflict":
                reporter = self._require_reporter(body)
                event = service.resolve_conflict(
                    parts[1], reporter, body["payload"], body.get("reason", ""),
                )
                self._send_json(200, _public_event(event))

            elif parts == ["scans"]:
                record = service.buffer_scan(
                    body["scan_id"], store=body["store"], lot_id=body["lot"],
                    scanned_at=body["scanned_at"], recovered_at=body.get("recovered_at"),
                    payload=body.get("payload"),
                )
                self._send_json(201, record)

            elif len(parts) == 3 and parts[0] == "scans" and parts[2] == "replay":
                reporter = self._require_reporter(body)
                event = service.replay_scan(parts[1], reporter)
                self._send_json(201, _public_event(event))

            elif len(parts) == 3 and parts[0] == "scans" and parts[2] == "resolve":
                reporter = self._require_reporter(body)
                result = service.resolve_scan_conflict(
                    parts[1], reporter, body["action"], note=body.get("note"),
                )
                self._send_json(200, result)

            elif parts == ["standards"]:
                reporter = self._require_reporter(body)
                standard = service.publish_standard(
                    body["standard_id"], body["title"], body["limits"],
                    effective_at=body["effective_at"], supersedes=body.get("supersedes"),
                    reporter=reporter,
                )
                self._send_json(201, standard)

            elif parts == ["recalls"]:
                reporter = self._require_reporter(body)
                recall = service.issue_recall(
                    body["recall_id"], body["reason"], body["source_lots"],
                    effective_at=body["effective_at"], level=body.get("level", "full"),
                    reporter=reporter,
                )
                self._send_json(201, recall)

            elif parts == ["seizures"]:
                reporter = self._require_reporter(body)
                event = service.seize(
                    body["lot"], reporter, reason=body["reason"], case_id=body["case_id"],
                    occurred_at=body.get("occurred_at"),
                )
                self._send_json(201, _public_event(event))

            elif parts == ["cases"]:
                reporter = self._require_reporter(body)
                case = service.close_case(
                    body["case_id"], body["title"], set(body["lot_ids"]), reporter,
                    closed_at=body.get("closed_at"),
                )
                self._send_json(201, case)

            elif parts == ["receipts"]:
                reporter = self._require_reporter(body)
                record = service.register_consumer_sale(
                    body["receipt"], body["lots"], store=body["store"],
                    sold_at=body["sold_at"], consumer=body.get("consumer"), reporter=reporter,
                )
                self._send_json(201, record)

            else:
                self._send_json(404, {"error": "not_found", "message": "未知路由"})

    return Handler


def _public_event(event):
    """响应中不回传内部去重指纹。"""
    data = dict(event)
    data.pop("fingerprint", None)
    return data


# 进程级单例账本（内存实现，重启清空；接口设计可直接替换为持久化后端）
service = TraceabilityService()
Handler = build_handler(service)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 领域冒烟：采收 -> 分装 -> 溯源 -> 脱敏摘要，全链路无异常
        probe = TraceabilityService()
        checker = {"role": "enterprise", "party": "check"}
        probe.submit_event(
            "harvest", checker,
            {"lot": "CHECK", "product": "自检样品", "quantity": 1, "unit": "kg",
             "packaging": "bulk", "farm": "自检产地", "producer": "自检主体"},
        )
        probe.repack(checker, inputs=[{"lot": "CHECK", "consumed": 1}],
                     outputs=[{"lot": "CHECK-OUT", "quantity": 1, "packaging": "case"}])
        assert probe.trace_origin("CHECK-OUT")["roots"][0]["lot"] == "CHECK"
        assert probe.public_summary("CHECK-OUT")["lot_ref"].startswith("L-")
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
