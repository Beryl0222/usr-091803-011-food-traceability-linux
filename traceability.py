"""食品全链条追溯领域核心。

覆盖产地采收、冷链运输温控、分装（拆分/混装）、检验、销售、退回、
销毁与扫码事件，维护箱、托盘、散装批次在形态变化后的来源关系。

关键规则：

* 同一事实被农业、市场监管、卫生部门或平台重复上报时，只形成一条事件
  记录，多方上报作为佐证附在同一条记录上；口径不一致时显式标记分歧，
  由执法方裁定，任何一方都不能静默覆盖。
* 企业只能修改自己尚未确认的草稿；确认后只能追加，不能改写。
* 检验结论在确认时冻结当时适用的标准版本快照；新标准只对生效后的
  判断有效，不倒改已结案的记录。
* 召回按决定生效时点圈定批次范围，并实时列出仍需封存的去向。
* 门店离线期间的扫码在恢复联网后批量同步；与既有链冲突的扫码生成
  显式冲突单，不静默丢弃也不静默覆盖。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------------------
# 异常


class TraceError(Exception):
    """领域错误，HTTP 层映射为 400。"""

    status = 400


class NotFound(TraceError):
    status = 404


class Forbidden(TraceError):
    status = 403


class StateConflict(TraceError):
    status = 409


# ---------------------------------------------------------------------------
# 工具函数

# 上报口径中不参与“同一事实”比对的噪声字段
_NOISE_KEYS = {"note", "source_channel", "reporter_name"}


def parse_time(value) -> float:
    """接受 epoch 数字或 ISO 8601 字符串，统一为 epoch 秒。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            raise TraceError(f"无法解析时间：{value!r}")
    raise TraceError("时间必须是 epoch 数字或 ISO 8601 字符串")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _comparable_payload(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k not in _NOISE_KEYS}


def jsonable(value):
    """把领域对象转为可 JSON 序列化的结构（集合转有序列表）。"""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, set):
        return [jsonable(v) for v in sorted(value, key=str)]
    return value


# ---------------------------------------------------------------------------
# 存储


class Store:
    """内存存储；生产环境可替换为持久化实现，接口保持不变。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.parties: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.documents: dict[str, dict] = {}
        self.standards: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.fact_index: dict[str, str] = {}  # fact_key -> event_id
        self.op_index: dict[str, str] = {}  # 客户端幂等键 -> event_id
        self.receipts: dict[str, dict] = {}
        self.recalls: dict[str, dict] = {}
        self.conflicts: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        self._counters: dict[str, int] = defaultdict(int)

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}{self._counters[prefix]:04d}"


# ---------------------------------------------------------------------------
# 领域服务


class TraceService:
    # 支持的事件类型
    EVENT_TYPES = {
        "harvest",  # 采收
        "transport",  # 运输温控
        "repack",  # 分装（拆分/混装）
        "inspection",  # 检验
        "transfer",  # 调拨/配送到店
        "sale",  # 销售（生成消费凭证）
        "return",  # 退回
        "destruction",  # 销毁
        "scan",  # 扫码观测
        "seal",  # 封存
    }

    def __init__(self, store: Store | None = None):
        self.store = store or Store()

    # -- 内部工具 ----------------------------------------------------------

    def _party(self, party_id: str) -> dict:
        party = self.store.parties.get(party_id)
        if party is None:
            raise NotFound(f"责任主体不存在：{party_id}")
        return party

    def _batch(self, batch_id: str) -> dict:
        batch = self.store.batches.get(batch_id)
        if batch is None:
            raise NotFound(f"批次不存在：{batch_id}")
        return batch

    def _event(self, event_id: str) -> dict:
        event = self.store.events.get(event_id)
        if event is None:
            raise NotFound(f"事件不存在：{event_id}")
        return event

    def _authenticate(self, token: str | None) -> dict:
        if not token:
            raise Forbidden("缺少访问令牌")
        party_id = self.store.tokens.get(token)
        if party_id is None:
            raise Forbidden("令牌无效")
        return self._party(party_id)

    @staticmethod
    def _fact_key(event_type: str, payload: dict) -> str:
        """同一事实的归并键。

        优先使用权威单据号（检验报告号、销毁单号、消费凭证号）；没有
        单据号时退化为 类型+发生时间（按分钟归整）+完整规范口径 的指纹，
        只有各项内容都相同的上报才会并为一条；需要区分同分钟的多笔同型
        事件时，上报方可显式给出 batch_id / 客户端幂等键。
        """
        source_ref = (payload.get("report_no") or payload.get("destruction_no")
                      or payload.get("receipt_no"))
        batches = payload.get("batch_id")
        if batches is None:
            batches = tuple(sorted(payload.get("batch_ids", [])))
        if source_ref:
            basis = [event_type, batches, source_ref]
        else:
            at_bucket = int(float(payload["at"]) // 60)
            basis = [event_type, at_bucket, _comparable_payload(payload)]
        digest = hashlib.sha1(_canonical(basis).encode("utf-8")).hexdigest()[:16]
        return f"{event_type}:{digest}"

    def _create_batch(self, data: dict, harvest_event_id: str | None,
                      created_at: float) -> dict:
        batch_id = data.get("batch_id") or self.store.next_id("B")
        if batch_id in self.store.batches:
            raise StateConflict(f"批次编号已存在：{batch_id}")
        batch = {
            "id": batch_id,
            "kind": data.get("kind", "bulk"),  # case 箱 / pallet 托盘 / bulk 散装
            "product": data["product"],
            "qty": float(data["qty"]),
            "unit": data.get("unit", "kg"),
            "origin": data.get("origin"),
            "harvest_event_id": harvest_event_id,
            "created_at": created_at,
            "parents": [],  # [{batch_id, qty, event_id}]
            "children": [],
        }
        self.store.batches[batch_id] = batch
        return batch

    # -- 责任主体与单据 ----------------------------------------------------

    def register_party(self, name: str, party_type: str,
                       contact: str | None = None) -> dict:
        """登记责任主体，返回身份与访问令牌（只在登记时出现一次）。"""
        if not name or not party_type:
            raise TraceError("主体名称与类型必填")
        with self.store.lock:
            party_id = self.store.next_id("P")
            token = secrets.token_hex(16)
            party = {
                "id": party_id,
                "name": name,
                "type": party_type,  # farm/carrier/packhouse/lab/store/platform/authority
                "contact": contact,
                "active": True,
                "created_at": datetime.now().timestamp(),
            }
            self.store.parties[party_id] = party
            self.store.tokens[token] = party_id
            return {**self._public_party(party), "token": token}

    @staticmethod
    def _public_party(party: dict) -> dict:
        return {k: party[k] for k in ("id", "name", "type", "active")}

    def register_document(self, token: str | None, subject_id: str,
                          doc_type: str, doc_no: str, issuer: str,
                          issued_at, expires_at=None, is_regulator=False) -> dict:
        """登记许可、检测报告、销毁证明等单据。

        不同部门互不相认的单据在这里统一挂到责任主体与事件上，
        成为同一条证据链上的节点；登记不代表背书，结论以检验/裁决事件为准。
        """
        caller = self._authenticate(token) if token else None
        if caller is None and not is_regulator:
            raise Forbidden("登记单据需要主体令牌或执法身份")
        if caller is not None and caller["id"] != subject_id and not is_regulator:
            raise Forbidden("只能为自己登记单据")
        self._party(subject_id)
        with self.store.lock:
            doc_id = self.store.next_id("D")
            doc = {
                "id": doc_id,
                "type": doc_type,  # license/inspection_report/destruction_cert/...
                "doc_no": doc_no,
                "issuer": issuer,
                "subject_id": subject_id,
                "issued_at": parse_time(issued_at),
                "expires_at": parse_time(expires_at) if expires_at else None,
                "registered_by": caller["id"] if caller else None,
            }
            self.store.documents[doc_id] = doc
            return dict(doc)

    # -- 标准版本 ----------------------------------------------------------

    def publish_standard(self, code: str, version: str, title: str,
                         effective_at, expires_at=None) -> dict:
        """发布标准版本。同一 code 可有多个版本，按时点选用。"""
        if not code or not version:
            raise TraceError("标准代号与版本号必填")
        effective = parse_time(effective_at)
        with self.store.lock:
            for std in self.store.standards.values():
                if std["code"] == code and std["version"] == version:
                    raise StateConflict(f"标准版本已存在：{code} {version}")
            std_id = self.store.next_id("STD")
            standard = {
                "id": std_id,
                "code": code,
                "version": version,
                "title": title,
                "effective_at": effective,
                "expires_at": parse_time(expires_at) if expires_at else None,
            }
            self.store.standards[std_id] = standard
            return dict(standard)

    def applicable_standard(self, code: str, at: float) -> dict | None:
        """返回某一时点适用的标准版本：已生效且未失效的最新版本。"""
        candidates = [
            s for s in self.store.standards.values()
            if s["code"] == code
            and s["effective_at"] <= at
            and (s["expires_at"] is None or at < s["expires_at"])
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s["effective_at"])

    def _freeze_standard(self, payload: dict, at: float) -> dict | None:
        """事件确认时冻结适用标准快照；事件直接指定标准 id 时也校验时点。"""
        std = None
        if payload.get("standard_id"):
            std = self.store.standards.get(payload["standard_id"])
            if std is None:
                raise TraceError("引用的标准不存在")
            if std["effective_at"] > at:
                raise TraceError("事件发生时该标准尚未生效，不能据此判定")
        elif payload.get("standard_code"):
            std = self.applicable_standard(payload["standard_code"], at)
        if std is None:
            return None
        return {k: std[k] for k in ("id", "code", "version", "title", "effective_at")}

    # -- 事件上报（含跨部门同一事实归并） ----------------------------------

    def report_event(self, token: str, event_type: str, data: dict,
                     client_op_id: str | None = None) -> dict:
        """上报一个追溯事件。

        相同 fact_key 的重复上报不会产生新记录，而是在原事件上追加一条
        佐证（attestation）；口径冲突时把事件置为 disputed，等待裁定。
        """
        reporter = self._authenticate(token)
        if event_type not in self.EVENT_TYPES:
            raise TraceError(f"不支持的事件类型：{event_type}")
        data = dict(data)
        data["at"] = parse_time(data["at"])

        with self.store.lock:
            # 校验在锁内完成，避免并发上报对同一批次结构产生竞态
            self._validate_event(event_type, data)
            # 客户端幂等：断网重试同一扫码/上报不会重复落账
            if client_op_id:
                existing = self.store.op_index.get(client_op_id)
                if existing:
                    return dict(self.store.events[existing])

            fact_key = self._fact_key(event_type, data)
            duplicated = self.store.fact_index.get(fact_key)

            if duplicated is None:
                event = self._build_event(event_type, data, reporter, fact_key)
                if reporter["type"] == "authority":
                    # 监管部门上报的事实提交即正式；企业上报先存草稿待确认
                    event["status"] = "confirmed"
                    if event_type == "inspection":
                        event["standard_snapshot"] = self._freeze_standard(
                            event["payload"], event["at"])
                self.store.events[event["id"]] = event
                self.store.fact_index[fact_key] = event["id"]
                self._apply_structure(event)
            else:
                event = self.store.events[duplicated]
                self._append_attestation(event, reporter, data)

            if client_op_id:
                self.store.op_index[client_op_id] = event["id"]
            return dict(event)

    def _validate_event(self, event_type: str, data: dict):
        if "at" not in data:
            raise TraceError("事件缺少发生时间 at")
        if event_type == "harvest":
            for key in ("product", "qty"):
                if key not in data:
                    raise TraceError(f"采收事件缺少 {key}")
        elif event_type == "repack":
            if not data.get("inputs") or not data.get("outputs"):
                raise TraceError("分装事件必须给出 inputs 与 outputs")
            input_ids = set()
            input_total = 0.0
            for item in data["inputs"]:
                self._batch(item["batch_id"])
                input_ids.add(item["batch_id"])
                input_total += float(item["qty"])
            output_total = 0.0
            for out in data["outputs"]:
                output_total += float(out["qty"])
                allocated = 0.0
                for src in out.get("sources", []):
                    if src["batch_id"] not in input_ids:
                        raise TraceError(
                            f"产出 {out.get('batch_id')} 的来源 {src['batch_id']} "
                            "不在投入批次中")
                    allocated += float(src["qty"])
                if out.get("sources") and abs(allocated - float(out["qty"])) > 0.0001:
                    raise TraceError(
                        f"产出 {out.get('batch_id')} 的来源投入量 "
                        f"{allocated} 与产出量 {out['qty']} 不符")
            if abs(output_total - input_total) > 0.0001 and len(input_ids) > 1:
                # 多来源混装必须账量守恒；单一来源的损耗另由损耗事件登记
                raise TraceError(
                    f"分装投入 {input_total} 与产出 {output_total} 数量不符")
        elif event_type in ("inspection", "destruction"):
            self._batch(data["batch_id"])
            if event_type == "inspection" and data.get("result") not in ("pass", "fail"):
                raise TraceError("检验结论 result 必须为 pass 或 fail")
        elif event_type in ("transfer", "sale", "return"):
            self._batch(data["batch_id"])
            if "qty" not in data:
                raise TraceError(f"{event_type} 事件缺少 qty")
        elif event_type == "transport":
            ids = data.get("batch_ids", [])
            if not ids:
                raise TraceError("运输事件必须给出 batch_ids")
            for bid in ids:
                self._batch(bid)

    def _build_event(self, event_type, data, reporter, fact_key) -> dict:
        event_id = self.store.next_id("E")
        # doc_ids 是证据挂载，不参与“同一事实”的口径比对
        payload = {k: v for k, v in data.items() if k != "doc_ids"}
        event = {
            "id": event_id,
            "type": event_type,
            "at": float(data["at"]),
            "recorded_at": datetime.now().timestamp(),
            "owner": reporter["id"],
            "status": "draft",  # 企业先存草稿，确认后不可改
            "fact_key": fact_key,
            "payload": payload,
            "attestations": [
                {
                    "party_id": reporter["id"],
                    "reporter_name": reporter["name"],
                    "reported_at": datetime.now().timestamp(),
                    "payload": _comparable_payload(payload),
                }
            ],
            "disputed": False,
            "dispute_resolution": None,
            "doc_ids": list(data.get("doc_ids", [])),
            "standard_snapshot": None,
            "amendments": [],
            "op_ids": [],
        }
        if event_type == "sale":
            receipt_no = data.get("receipt_no") or self.store.next_id("R")
            if receipt_no in self.store.receipts:
                raise StateConflict(f"消费凭证号已存在：{receipt_no}")
            event["payload"]["receipt_no"] = receipt_no
            event["attestations"][0]["payload"]["receipt_no"] = receipt_no
            self.store.receipts[receipt_no] = {
                "receipt_no": receipt_no,
                "event_id": event_id,
                "batch_id": data["batch_id"],
                "qty": float(data["qty"]),
                "at": float(data["at"]),
                "store_id": reporter["id"],
            }
        return event

    def _append_attestation(self, event: dict, reporter: dict, data: dict):
        # 已确认的事实仍可接收其他部门佐证，但绝不改写规范内容。
        # 以首条佐证（上报方的原始口径）为比对基准，event.payload 中
        # 系统回填的字段（如采收生成的 batch_id）不参与口径比对。
        incoming = _comparable_payload(data)
        canonical = event["attestations"][0]["payload"]
        if incoming != canonical and event["dispute_resolution"] is None:
            event["disputed"] = True
        event["attestations"].append({
            "party_id": reporter["id"],
            "reporter_name": reporter["name"],
            "reported_at": datetime.now().timestamp(),
            "payload": incoming,
            "conflicts_with_canonical": incoming != canonical,
        })
        for doc_id in data.get("doc_ids", []):
            if doc_id not in event["doc_ids"]:
                event["doc_ids"].append(doc_id)

    def resolve_dispute(self, event_id: str, chosen_party_id: str,
                        reason: str) -> dict:
        """执法方对同一事实上的口径分歧作出裁定。"""
        with self.store.lock:
            event = self._event(event_id)
            choices = {a["party_id"] for a in event["attestations"]}
            if chosen_party_id not in choices:
                raise TraceError("被采信方必须是该事实的上报方之一")
            chosen = next(a for a in event["attestations"]
                          if a["party_id"] == chosen_party_id)
            event["payload"].update(chosen["payload"])
            event["disputed"] = False
            event["dispute_resolution"] = {
                "canonical_party_id": chosen_party_id,
                "reason": reason,
                "resolved_at": datetime.now().timestamp(),
            }
            # 被采信口径若含不同标准依据，按裁定口径重新冻结标准快照
            if event["type"] == "inspection" and event["status"] == "confirmed":
                event["standard_snapshot"] = self._freeze_standard(
                    event["payload"], event["at"])
            return dict(event)

    def _apply_structure(self, event: dict):
        """新事件落账时建立批次形态（采收产出、分装拆分/混装谱系）。"""
        p = event["payload"]
        if event["type"] == "harvest":
            batch = self._create_batch(p, event["id"], event["at"])
            # 回填系统生成的批次号，便于按事件查批次；它不属于上报口径，
            # 不写回 attestations，重复上报时不会被当成分歧字段。
            p["batch_id"] = batch["id"]
        elif event["type"] == "repack":
            raw_outputs = p["outputs"]
            outputs = [self._create_batch(out, None, event["at"])
                       for out in raw_outputs]
            inputs = p["inputs"]
            for raw, out in zip(raw_outputs, outputs):
                # 产出显式声明来源构成；未声明时（仅单一来源拆分）默认
                # 该产出全部来自唯一投入，避免混装被误连到不相干的批次。
                if raw.get("sources"):
                    links = [
                        {"batch_id": s["batch_id"], "qty": float(s["qty"])}
                        for s in raw["sources"]
                    ]
                elif len(inputs) == 1:
                    links = [{"batch_id": inputs[0]["batch_id"],
                              "qty": float(out["qty"])}]
                else:
                    raise TraceError(
                        f"混装产出 {out['id']} 必须通过 sources 声明来源构成")
                for edge in links:
                    out["parents"].append({**edge, "event_id": event["id"]})
                    self._batch(edge["batch_id"])["children"].append({
                        "batch_id": out["id"],
                        "qty": float(out["qty"]),
                        "event_id": event["id"],
                    })

    # -- 草稿与确认 --------------------------------------------------------

    def edit_draft(self, token: str, event_id: str, patch: dict) -> dict:
        """企业只能修改自己尚未确认的草稿。"""
        reporter = self._authenticate(token)
        with self.store.lock:
            event = self._event(event_id)
            if event["owner"] != reporter["id"]:
                raise Forbidden("只能修改本主体上报的事件")
            if event["status"] != "draft":
                raise StateConflict("事件已确认，不能修改；如有误请走追加更正")
            if event["type"] == "repack" and ("inputs" in patch or "outputs" in patch):
                raise TraceError("分装的拆分/混装结构已建立，草稿也不能改结构；"
                                 "请作废后重新上报")
            if "at" in patch:
                patch = dict(patch)
                patch["at"] = parse_time(patch["at"])
            event["amendments"].append({
                "from": dict(event["payload"]),
                "at": datetime.now().timestamp(),
            })
            event["payload"].update(patch)
            # 采收草稿的批次随草稿同步，确认后两边都冻结
            if event["type"] == "harvest":
                for batch in self.store.batches.values():
                    if batch.get("harvest_event_id") == event_id:
                        for key in ("product", "qty", "unit", "origin", "kind"):
                            if key in patch:
                                batch[key] = (float(patch[key])
                                              if key == "qty" else patch[key])
            return dict(event)

    def confirm_event(self, token: str, event_id: str) -> dict:
        """确认草稿：冻结适用标准快照，此后记录不可修改。"""
        reporter = self._authenticate(token)
        with self.store.lock:
            event = self._event(event_id)
            if event["owner"] != reporter["id"]:
                raise Forbidden("只能确认本主体上报的事件")
            if event["status"] != "draft":
                raise StateConflict("事件已确认")
            if event["disputed"]:
                raise StateConflict("事件存在未裁定的口径分歧，不能确认")
            self._assert_inputs_confirmed(event)
            event["status"] = "confirmed"
            if event["type"] == "inspection":
                event["standard_snapshot"] = self._freeze_standard(
                    event["payload"], event["at"])
            return dict(event)

    def _assert_inputs_confirmed(self, event: dict):
        """确认前要求事件涉及的批次都来自已确认的成形事件。

        防止未确认的采收/分装产出被提前调拨、检验或销售，造成账面亏空。
        """
        p = event["payload"]

        def batch_ready(bid, seen):
            if bid in seen:
                return
            seen.add(bid)
            batch = self.store.batches.get(bid)
            if batch is None:
                raise TraceError(f"批次不存在：{bid}")
            forming = batch.get("harvest_event_id")
            # 当前事件自身正在成形的批次不算未就绪的上游
            if forming and forming != event["id"]:
                fe = self.store.events.get(forming)
                if fe is None or fe["status"] != "confirmed":
                    raise StateConflict(
                        f"批次 {bid} 的采收记录尚未确认，不能确认本事件")
            for edge in batch["parents"]:
                if edge["event_id"] == event["id"]:
                    continue
                pe = self.store.events.get(edge["event_id"])
                if pe is None or pe["status"] != "confirmed":
                    raise StateConflict(
                        f"批次 {bid} 的分装记录尚未确认，不能确认本事件")
                batch_ready(edge["batch_id"], seen)

        seen = set()
        if event["type"] == "harvest":
            return  # 采收是链路起点，没有需要先确认的上游
        if event["type"] == "repack":
            for inp in p["inputs"]:
                batch_ready(inp["batch_id"], seen)
        elif event["type"] == "transport":
            for bid in p.get("batch_ids", []):
                batch_ready(bid, seen)
        else:
            if p.get("batch_id"):
                batch_ready(p["batch_id"], seen)

    # -- 离线扫码同步与冲突 ------------------------------------------------

    def sync_scans(self, token: str, scans: list[dict]) -> dict:
        """门店恢复联网后批量回补扫码记录。

        返回 {accepted, conflicts}：与既有链矛盾的扫码不会被丢弃，
        而是生成待处理的显式冲突单。
        """
        store_party = self._authenticate(token)
        accepted, conflicts = [], []
        with self.store.lock:
            holdings_now, _, _ = self._holdings()
            for raw in scans:
                scan = dict(raw)
                op_id = scan.get("op_id")
                if op_id and op_id in self.store.op_index:
                    accepted.append(self.store.op_index[op_id])
                    continue
                at = parse_time(scan["at"])
                batch_id = scan.get("batch_id")

                kind = self._detect_scan_conflict(
                    scan, at, batch_id, store_party, holdings_now)
                if kind:
                    conflict = {
                        "id": self.store.next_id("C"),
                        "kind": kind,
                        "scan_op_id": op_id,
                        "scan": {**scan, "at": at, "party_id": store_party["id"]},
                        "batch_id": batch_id,
                        "status": "open",
                        "resolution": None,
                        "created_at": datetime.now().timestamp(),
                    }
                    self.store.conflicts[conflict["id"]] = conflict
                    conflicts.append(dict(conflict))
                    continue

                payload = {
                    "at": at,
                    "batch_id": batch_id,
                    "action": scan.get("action", "observe"),
                    "device_id": scan.get("device_id"),
                    "note": scan.get("note"),
                }
                # 离线期间的卖出扫码恢复后按销售事件入账（生成消费凭证并
                # 核减账面）；其余动作作为扫码观测事件入账。
                event_type = "sale" if scan.get("action") == "sell" else "scan"
                if event_type == "sale":
                    if "qty" not in scan:
                        raise TraceError("卖出扫码必须包含 qty")
                    payload["qty"] = float(scan["qty"])
                    # 提前确定凭证号，保证不同卖出扫码不会被归并成同一事实
                    payload["receipt_no"] = (
                        scan.get("receipt_no") or self.store.next_id("R"))
                    payload.pop("action", None)
                fact_key = self._fact_key(event_type, payload)
                if fact_key in self.store.fact_index:
                    event_id = self.store.fact_index[fact_key]
                else:
                    event = self._build_event(event_type, payload, store_party,
                                              fact_key)
                    event["status"] = "confirmed"  # 扫码是客观观测，直接入账
                    event["late_evidence"] = self._is_late(scan, at)
                    self.store.events[event["id"]] = event
                    self.store.fact_index[fact_key] = event["id"]
                    event_id = event["id"]
                if op_id:
                    self.store.op_index[op_id] = event_id
                accepted.append(event_id)
        return {"accepted": accepted, "conflicts": conflicts}

    def _detect_scan_conflict(self, scan, at, batch_id, store_party,
                              holdings_now) -> str | None:
        if batch_id not in self.store.batches:
            return "unknown_batch"
        # 已销毁的批次又出现扫码
        destroyed = self._destroyed_qty(batch_id)
        produced = self._produced_qty(batch_id)
        if destroyed >= produced and any(
                e["type"] == "destruction" and e["at"] <= at
                for e in self._batch_events(batch_id)):
            return "batch_already_destroyed"
        # 扫码时点批次账面上在别的持有人处
        holders = holdings_now.get(batch_id, {})
        other = {h: q for h, q in holders.items()
                 if h != store_party["id"] and q > 0}
        if other and holders.get(store_party["id"], 0) <= 0:
            return "location_mismatch"
        # 召回生效后仍有出库/销售动作
        if scan.get("action") in ("sell", "ship"):
            for recall in self.store.recalls.values():
                if recall["status"] == "active" and batch_id in recall["scope_batches"]:
                    if at >= recall["effective_at"]:
                        return "recall_violation"
        return None

    @staticmethod
    def _is_late(scan, at) -> bool:
        reported_at = parse_time(scan["reported_at"]) if scan.get("reported_at") else at
        return at + 300 < reported_at

    def resolve_conflict(self, conflict_id: str, decision: str,
                         reason: str) -> dict:
        """处理扫码冲突：accept_scan 采信扫码 / reject_scan 否决 /
        confirm_existing 维持既有链。处理过程留痕。"""
        if decision not in ("accept_scan", "reject_scan", "confirm_existing"):
            raise TraceError("decision 取值非法")
        with self.store.lock:
            conflict = self.store.conflicts.get(conflict_id)
            if conflict is None:
                raise NotFound(f"冲突单不存在：{conflict_id}")
            if conflict["status"] != "open":
                raise StateConflict("冲突单已处理")
            conflict["status"] = "resolved"
            conflict["resolution"] = {
                "decision": decision,
                "reason": reason,
                "resolved_at": datetime.now().timestamp(),
            }
            if decision == "accept_scan":
                scan = conflict["scan"]
                known_batch = conflict["batch_id"] in self.store.batches
                # 被采信的卖出扫码按销售入账（生成消费凭证、纳入证据链）；
                # 未知批次（如疑似假冒标签）只作扫码证据，不生成销售账。
                if scan.get("action") == "sell" and known_batch:
                    payload = {
                        "at": scan["at"],
                        "batch_id": conflict["batch_id"],
                        "qty": float(scan["qty"]),
                        "receipt_no": scan.get("receipt_no") or self.store.next_id("R"),
                        "note": f"冲突 {conflict_id} 裁定采信：{reason}",
                    }
                    event_type = "sale"
                else:
                    payload = {
                        "at": scan["at"],
                        "batch_id": conflict["batch_id"],
                        "action": scan.get("action", "observe"),
                        "note": f"冲突 {conflict_id} 裁定采信：{reason}",
                    }
                    event_type = "scan"
                event = self._build_event(
                    event_type, payload, self._party(scan["party_id"]),
                    self.store.next_id("FK"))
                event["status"] = "confirmed"
                event["conflict_id"] = conflict_id
                event["late_evidence"] = True
                self.store.events[event["id"]] = event
                self.store.fact_index[event["fact_key"]] = event["id"]
                conflict["resolution"]["event_id"] = event["id"]
            return dict(conflict)

    # -- 谱系 --------------------------------------------------------------

    def lineage(self, batch_id: str, direction: str = "both") -> dict:
        """返回批次的上游来源与/或下游去向（拆分、混装逐跳展开）。"""
        with self.store.lock:
            self._batch(batch_id)
            result = {"batch_id": batch_id}
            if direction in ("up", "both"):
                result["upstream"] = self._walk(batch_id, "parents")
            if direction in ("down", "both"):
                result["downstream"] = self._walk(batch_id, "children")
            return result

    def _walk(self, batch_id: str, edge_name: str, confirmed_only=True) -> list[dict]:
        seen = set()
        out = []

        def visit(bid, depth):
            batch = self._batch(bid)
            for edge in batch[edge_name]:
                forming = self.store.events.get(edge["event_id"])
                if confirmed_only and (
                        forming is None or forming["status"] != "confirmed"):
                    # 未确认的分装不构成既成的批次关系
                    continue
                other = edge["batch_id"]
                node = {"batch_id": other, "depth": depth,
                        "via_event": edge["event_id"], "qty": edge["qty"]}
                out.append(node)
                if other not in seen:
                    seen.add(other)
                    visit(other, depth + 1)

        visit(batch_id, 1)
        return out

    def ancestors(self, batch_id: str) -> set[str]:
        return {n["batch_id"] for n in self._walk(batch_id, "parents")}

    def descendants(self, batch_id: str) -> set[str]:
        return {n["batch_id"] for n in self._walk(batch_id, "children")}

    # -- 库存/去向账面 ------------------------------------------------------

    def _confirmed_events(self) -> list[dict]:
        return sorted(
            (e for e in self.store.events.values() if e["status"] == "confirmed"),
            key=lambda e: (e["at"], e["recorded_at"]),
        )

    def _batch_events(self, batch_id: str) -> list[dict]:
        return [e for e in self.store.events.values()
                if self._event_touches(e, batch_id)]

    @staticmethod
    def _event_touches(event: dict, batch_id: str) -> bool:
        p = event["payload"]
        if p.get("batch_id") == batch_id:
            return True
        if batch_id in p.get("batch_ids", []):
            return True
        if event["type"] == "repack":
            if any(i.get("batch_id") == batch_id for i in p.get("inputs", [])):
                return True
            if any(o.get("batch_id") == batch_id for o in p.get("outputs", [])):
                return True
        return False

    def _produced_qty(self, batch_id: str) -> float:
        """批次的总形成量：采收批次取初始量，分装产出取分装产出量。"""
        batch = self._batch(batch_id)
        if batch.get("harvest_event_id"):
            return float(batch["qty"])
        total = sum(
            float(out["qty"])
            for e in self._batch_events(batch_id)
            if e["type"] == "repack"
            for out in e["payload"].get("outputs", [])
            if out.get("batch_id") == batch_id
        )
        return total or float(batch["qty"])

    def _destroyed_qty(self, batch_id: str) -> float:
        return sum(
            float(e["payload"]["qty"])
            for e in self._batch_events(batch_id)
            if e["type"] == "destruction"
        )

    def _repacked_out_qty(self, batch_id: str) -> float:
        """作为分装投入被转换掉的数量。"""
        return sum(
            float(inp["qty"])
            for e in self._batch_events(batch_id)
            if e["type"] == "repack"
            for inp in e["payload"].get("inputs", [])
            if inp.get("batch_id") == batch_id
        )

    def _deduct(self, holdings, batch_id, qty, prefer=None):
        holders = holdings[batch_id]
        if prefer and holders.get(prefer, 0) >= qty:
            holders[prefer] -= qty
            return
        remaining = qty
        order = sorted(holders, key=lambda h: 0 if h == prefer else 1)
        for holder in order:
            if remaining <= 0:
                break
            take = min(holders[holder], remaining)
            holders[holder] -= take
            remaining -= take
        if remaining > 0.0001:
            # 账面缺口：记录在 None 名下，供追查而不是静默抹平
            holders[None] = holders.get(None, 0) - remaining

    def _holdings(self, as_of: float | None = None):
        """根据已确认事件重放账面：{批次: {持有主体: 数量}}、封存量与扫码观测。

        as_of 给出时只重放该时点之前的事件，用于召回生效时点的快照。
        """
        holdings: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        sealed: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        observations = []
        for event in self._confirmed_events():
            if as_of is not None and event["at"] > as_of:
                continue
            p = event["payload"]
            t = event["type"]
            if t == "harvest":
                bid = next(iter(
                    b for b in self.store.batches.values()
                    if b.get("harvest_event_id") == event["id"]), None)
                if bid:
                    holdings[bid["id"]][event["owner"]] += float(p["qty"])
            elif t == "repack":
                for inp in p["inputs"]:
                    self._deduct(holdings, inp["batch_id"], float(inp["qty"]),
                                 prefer=event["owner"])
                for out in p["outputs"]:
                    holdings[out["batch_id"]][event["owner"]] += float(out["qty"])
            elif t == "transfer":
                self._deduct(holdings, p["batch_id"], float(p["qty"]),
                             prefer=p.get("from"))
                holdings[p["batch_id"]][p.get("to", event["owner"])] += float(p["qty"])
            elif t == "sale":
                self._deduct(holdings, p["batch_id"], float(p["qty"]),
                             prefer=event["owner"])
            elif t == "return":
                if p.get("from"):
                    self._deduct(holdings, p["batch_id"], float(p["qty"]),
                                 prefer=p["from"])
                holdings[p["batch_id"]][p.get("to", event["owner"])] += float(p["qty"])
            elif t == "destruction":
                self._deduct(holdings, p["batch_id"], float(p["qty"]),
                             prefer=p.get("holder_id", event["owner"]))
            elif t == "seal":
                self._deduct(holdings, p["batch_id"], float(p["qty"]),
                             prefer=p.get("holder_id"))
                sealed[p["batch_id"]][p["holder_id"]] += float(p["qty"])
            elif t == "scan":
                observations.append({
                    "batch_id": p["batch_id"],
                    "holder_id": event["owner"],
                    "at": event["at"],
                    "event_id": event["id"],
                })
        return holdings, sealed, observations

    # -- 召回与封存 --------------------------------------------------------

    def create_recall(self, reason: str, effective_at,
                      root_batch_ids: list[str] | None = None,
                      criteria: dict | None = None,
                      public_guidance: str | None = None) -> dict:
        """按生效时点发起召回，圈定范围并立即列出仍需封存的去向。

        范围集合在决定时固化（谱系快照）；封存清单随后续封存/销毁动态收敛。
        """
        effective = parse_time(effective_at)
        with self.store.lock:
            roots = list(root_batch_ids or [])
            if criteria:
                lo = parse_time(criteria["harvested_from"])
                hi = parse_time(criteria["harvested_to"])
                for bid, batch in self.store.batches.items():
                    harvest = self.store.events.get(
                        batch.get("harvest_event_id", ""), None)
                    if harvest is None or harvest["status"] != "confirmed":
                        continue
                    hat = harvest["at"]
                    if (batch["product"] == criteria["product"]
                            and lo <= hat <= hi and bid not in roots):
                        roots.append(bid)
            for bid in roots:
                self._batch(bid)

            scope = set(roots)
            relations = {bid: "root" for bid in roots}
            for root in roots:
                for desc in self.descendants(root):
                    scope.add(desc)
                    relations.setdefault(desc, "descendant")

            # 生效时点快照：当时仍在流通环节的数量
            holdings_then, _, _ = self._holdings(as_of=effective)
            snapshot = {}
            for bid in sorted(scope):
                holders = {h: q for h, q in holdings_then.get(bid, {}).items()
                           if h and q > 0}
                if holders:
                    snapshot[bid] = holders

            recall_id = self.store.next_id("RC")
            recall = {
                "id": recall_id,
                "reason": reason,
                "public_guidance": public_guidance or "请停止食用并联系购买门店退回。",
                "effective_at": effective,
                "created_at": datetime.now().timestamp(),
                "status": "active",
                "root_batch_ids": roots,
                "scope_batches": sorted(scope),
                "scope_relations": relations,
                "commerce_snapshot": snapshot,
            }
            self.store.recalls[recall_id] = recall
            recall["pending_seals"] = self._pending_seals(recall)
            return dict(recall)

    def _pending_seals(self, recall: dict) -> list[dict]:
        holdings, sealed, observations = self._holdings()
        pending = []
        for bid in recall["scope_batches"]:
            holders = holdings.get(bid, {})
            for holder, qty in holders.items():
                already = sealed.get(bid, {}).get(holder, 0)
                left = qty - already
                if holder and left > 0.0001:
                    pending.append({
                        "batch_id": bid,
                        "holder_id": holder,
                        "qty": round(left, 6),
                        "source": "ledger",
                        "relation": recall["scope_relations"].get(bid),
                    })
            # 账面已平但仍有账实不符的余量时，只采信召回生效后的失联门店
            # 扫码，把它作为“估算去向”显式列出；更早的扫码不能代表召回时
            # 位置，已经登记过封存的观测去向也不再重复列出。
            if not any(h and q > 0 for h, q in holders.items()):
                unexplained = (self._produced_qty(bid)
                               - self._destroyed_qty(bid)
                               - self._repacked_out_qty(bid))
                sealed_here = sealed.get(bid, {})
                if unexplained > 0.0001:
                    latest = [o for o in observations
                              if o["batch_id"] == bid
                              and o["at"] >= recall["effective_at"]
                              and sealed_here.get(o["holder_id"], 0) <= 0]
                    if latest:
                        obs = max(latest, key=lambda o: o["at"])
                        pending.append({
                            "batch_id": bid,
                            "holder_id": obs["holder_id"],
                            "qty": None,
                            "source": "scan_observation",
                            "event_id": obs["event_id"],
                            "relation": recall["scope_relations"].get(bid),
                        })
        return pending

    def get_recall(self, recall_id: str) -> dict:
        with self.store.lock:
            recall = self.store.recalls.get(recall_id)
            if recall is None:
                raise NotFound(f"召回不存在：{recall_id}")
            result = dict(recall)
            result["pending_seals"] = self._pending_seals(recall)
            return result

    def seal_batch(self, token: str | None, recall_id: str, batch_id: str,
                   holder_id: str, qty: float) -> dict:
        """登记封存动作，从待封存清单中核减。"""
        caller = self._authenticate(token) if token else None
        with self.store.lock:
            recall = self.store.recalls.get(recall_id)
            if recall is None:
                raise NotFound(f"召回不存在：{recall_id}")
            if batch_id not in recall["scope_batches"]:
                raise TraceError("该批次不在本次召回范围")
            if caller is not None and caller["type"] != "authority" \
                    and caller["id"] != holder_id:
                raise Forbidden("只能登记本主体名下的封存；代封请由执法方操作")
            pending = self._pending_seals(recall)
            entry = next((p for p in pending
                          if p["batch_id"] == batch_id
                          and p["holder_id"] == holder_id), None)
            if entry is None:
                raise StateConflict("该批次在此主体处已无待封存数量")
            if float(qty) <= 0:
                raise TraceError("封存数量必须大于 0")
            if entry["qty"] is not None and float(qty) - entry["qty"] > 0.0001:
                raise TraceError(
                    f"封存数量超过待封存数量：申报 {qty}，可封 {entry['qty']}")
            payload = {
                "at": datetime.now().timestamp(),
                "batch_id": batch_id,
                "holder_id": holder_id,
                "qty": float(qty),
                "recall_id": recall_id,
            }
            event = {
                "id": self.store.next_id("E"),
                "type": "seal",
                "at": payload["at"],
                "recorded_at": payload["at"],
                "owner": caller["id"] if caller else holder_id,
                "status": "confirmed",
                "fact_key": self.store.next_id("FK"),
                "payload": payload,
                "attestations": [],
                "disputed": False,
                "dispute_resolution": None,
                "doc_ids": [],
                "standard_snapshot": None,
                "amendments": [],
                "op_ids": [],
            }
            self.store.events[event["id"]] = event
            return self.get_recall(recall_id)

    # -- 结案 --------------------------------------------------------------

    def close_case(self, title: str, inspection_event_ids: list[str],
                   judgment: str) -> dict:
        """对执法案件结案：冻结当时的检验结论与标准快照。

        日后发布的新标准不会改变本结案记录。
        """
        with self.store.lock:
            frozen = []
            for eid in inspection_event_ids:
                event = self._event(eid)
                if event["type"] != "inspection":
                    raise TraceError(f"{eid} 不是检验事件")
                if event["status"] != "confirmed":
                    raise TraceError(f"{eid} 尚未确认，不能纳入结案")
                if event["disputed"]:
                    raise TraceError(f"{eid} 存在未裁定的口径分歧，不能纳入结案")
                frozen.append({
                    "event_id": eid,
                    "batch_id": event["payload"]["batch_id"],
                    "result": event["payload"]["result"],
                    "at": event["at"],
                    "standard_snapshot": dict(event["standard_snapshot"] or {}),
                })
            case = {
                "id": self.store.next_id("CASE"),
                "title": title,
                "judgment": judgment,
                "closed_at": datetime.now().timestamp(),
                "conclusions": frozen,
            }
            self.store.cases[case["id"]] = case
            return dict(case)

    # -- 消费凭证逆向追溯 --------------------------------------------------

    def trace_receipt(self, receipt_no: str) -> dict:
        """从一份消费凭证逆向导出责任主体、适用标准与完整证据链。"""
        with self.store.lock:
            receipt = self.store.receipts.get(receipt_no)
            if receipt is None:
                raise NotFound(f"消费凭证不存在：{receipt_no}")
            batch_id = receipt["batch_id"]
            chain_set = {batch_id} | self.ancestors(batch_id)

            events = [e for e in self.store.events.values()
                      if any(self._event_touches(e, b) for b in chain_set)
                      and e["status"] == "confirmed"]
            events.sort(key=lambda e: (e["at"], e["recorded_at"]))

            chain = []
            party_ids = set()
            standards = {}
            for event in events:
                party_ids.add(event["owner"])
                stage = self._stage_view(event, chain_set)
                chain.append(stage)
                if event.get("standard_snapshot"):
                    snap = event["standard_snapshot"]
                    standards[snap["id"]] = snap
                for doc_id in event.get("doc_ids", []):
                    doc = self.store.documents.get(doc_id)
                    if doc:
                        party_ids.add(doc["subject_id"])

            parties = []
            for pid in sorted(party_ids):
                party = self.store.parties.get(pid)
                if not party:
                    continue
                parties.append({
                    "id": pid,
                    "name": party["name"],
                    "type": party["type"],
                    "role_in_chain": self._party_role(party["type"]),
                    "documents": [
                        {k: d[k] for k in
                         ("id", "type", "doc_no", "issuer", "issued_at", "expires_at")}
                        for d in self.store.documents.values()
                        if d["subject_id"] == pid
                    ],
                })

            active_recalls = [
                self.get_recall(rid) for rid, r in self.store.recalls.items()
                if batch_id in r["scope_batches"] or chain_set & set(r["scope_batches"])
            ]
            pending = []
            for recall in active_recalls:
                if recall["status"] != "active":
                    continue
                pending.extend(p for p in recall["pending_seals"]
                               if p["batch_id"] in chain_set)

            return {
                "receipt_no": receipt_no,
                "consumer_batch_id": batch_id,
                "product": self._batch(batch_id)["product"],
                "sold_at": receipt["at"],
                "store_id": receipt["store_id"],
                "parties": parties,
                "standards_applied": list(standards.values()),
                "evidence_chain": chain,
                "active_recalls": [
                    {"id": r["id"], "reason": r["reason"],
                     "effective_at": r["effective_at"]}
                    for r in active_recalls if r["status"] == "active"
                ],
                "pending_seals": pending,
            }

    @staticmethod
    def _party_role(party_type: str) -> str:
        return {
            "farm": "种植/养殖产地",
            "carrier": "冷链承运方",
            "packhouse": "分装仓",
            "lab": "检验机构",
            "store": "餐饮/零售门店",
            "platform": "平台",
            "authority": "监管部门",
        }.get(party_type, party_type)

    def _stage_view(self, event: dict, chain_set: set[str]) -> dict:
        p = event["payload"]
        stage = {
            "event_id": event["id"],
            "type": event["type"],
            "at": event["at"],
            "party_id": event["owner"],
            "doc_ids": list(event.get("doc_ids", [])),
            "disputed": event["disputed"],
        }
        if event["type"] == "harvest":
            stage.update(product=p["product"], qty=p["qty"], unit=p.get("unit"),
                         origin=p.get("origin"))
        elif event["type"] == "transport":
            readings = p.get("readings", [])
            stage.update(
                batch_ids=[b for b in p.get("batch_ids", []) if b in chain_set],
                temp_excursion=bool(p.get("temp_excursion")) or any(
                    r.get("temp") is not None and (
                        r["temp"] > float(p.get("max_temp", 99))
                        or r["temp"] < float(p.get("min_temp", -99)))
                    for r in readings),
                readings=readings,
                route={"from": p.get("from_location"), "to": p.get("to_location")},
            )
        elif event["type"] == "repack":
            stage.update(mode=p.get("mode", "repack"),
                         inputs=p["inputs"], outputs=p["outputs"])
        elif event["type"] == "inspection":
            stage.update(batch_id=p["batch_id"], result=p["result"],
                         report_no=p.get("report_no"),
                         standard_snapshot=event.get("standard_snapshot"))
        elif event["type"] == "transfer":
            stage.update(batch_id=p["batch_id"], qty=p["qty"],
                         **{"from": p.get("from"), "to": p.get("to")})
        elif event["type"] == "sale":
            stage.update(batch_id=p["batch_id"], qty=p["qty"],
                         receipt_no=p.get("receipt_no"))
        elif event["type"] in ("return", "destruction", "seal"):
            stage.update(batch_id=p["batch_id"], qty=p["qty"],
                         reason=p.get("reason"))
        return stage

    # -- 公众脱敏摘要 ------------------------------------------------------

    _PUBLIC_EVENT_TYPES = {
        "harvest": "采收", "inspection": "检验", "recall": "召回",
        "destruction": "销毁", "sale": "销售",
    }

    def public_summary(self, batch_id: str | None = None,
                       receipt_no: str | None = None) -> dict:
        """面向公众的脱敏安全摘要：只讲食品安全结论，不暴露主体与单据细节。"""
        with self.store.lock:
            if receipt_no:
                trace = self.trace_receipt(receipt_no)
                batch_id = trace["consumer_batch_id"]
            batch = self._batch(batch_id)
            chain_set = {batch_id} | self.ancestors(batch_id) | self.descendants(batch_id)

            inspections = [
                e for e in self.store.events.values()
                if e["type"] == "inspection" and e["status"] == "confirmed"
                and e["payload"]["batch_id"] in chain_set
            ]
            failed = [e for e in inspections if e["payload"]["result"] == "fail"]
            recalls = [r for r in self.store.recalls.values()
                       if batch_id in r["scope_batches"] and r["status"] == "active"]

            if recalls:
                risk, guidance = "recalled", recalls[0]["public_guidance"]
            elif failed:
                risk, guidance = "unsafe", "该批次曾检出不合格，相关产品已依法处置。"
            elif inspections:
                risk, guidance = "tested_pass", "该批次检验合格。"
            else:
                risk, guidance = "no_public_data", "暂无该批次的公开检验信息。"

            timeline = []
            for e in sorted(inspections, key=lambda x: x["at"]):
                timeline.append({
                    "kind": self._PUBLIC_EVENT_TYPES["inspection"],
                    "at": e["at"],
                    "result": "合格" if e["payload"]["result"] == "pass" else "不合格",
                })
            for r in recalls:
                timeline.append({"kind": "召回", "at": r["effective_at"],
                                 "reason": r["reason"]})

            return {
                "product": batch["product"],
                "batch_hint": hashlib.sha1(
                    batch_id.encode("utf-8")).hexdigest()[:6] + "…",
                "risk_level": risk,
                "guidance": guidance,
                "timeline": sorted(timeline, key=lambda x: x["at"]),
            }

    def public_recall(self, recall_id: str) -> dict:
        with self.store.lock:
            recall = self.store.recalls.get(recall_id)
            if recall is None:
                raise NotFound(f"召回不存在：{recall_id}")
            products = sorted({
                self._batch(b)["product"] for b in recall["scope_batches"]
            })
            return {
                "id": recall["id"],
                "products": products,
                "reason": recall["reason"],
                "guidance": recall["public_guidance"],
                "effective_at": recall["effective_at"],
                "scope_batch_count": len(recall["scope_batches"]),
            }

    # -- 查询辅助 ----------------------------------------------------------

    def get_event(self, event_id: str) -> dict:
        return dict(self._event(event_id))

    def get_batch(self, batch_id: str) -> dict:
        return dict(self._batch(batch_id))

    def list_conflicts(self, status: str | None = None) -> list[dict]:
        return [dict(c) for c in self.store.conflicts.values()
                if status is None or c["status"] == status]

    def get_case(self, case_id: str) -> dict:
        case = self.store.cases.get(case_id)
        if case is None:
            raise NotFound(f"案件不存在：{case_id}")
        return dict(case)
