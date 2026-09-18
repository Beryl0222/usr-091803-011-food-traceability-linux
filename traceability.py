"""食品全链条追溯领域模型。

只依赖标准库，线程安全。核心概念：

* ``Lot``：追溯单元（箱/托盘/散装批次），带包装形态与数量、单位。
* ``TraceabilityService``：只增事件账本。事件描述采收、运输温控、分装、
  检验、销售、退回、销毁，以及拆分与混装形成的谱系。
* 事件先以 ``draft``（草稿）落入，企业可改；确认后冻结，重复上报同一事实
  只合并为一条记录并登记每个报送方。
* 事实冲突不静默覆盖：保留为同一事件下的冲突版本，需要显式裁决。
* 标准与召回均带生效/失效时点，判断仅依据事件发生时点；结案快照不可倒改。
* 门店失联期间的扫码在恢复后回放，与账本既有事实冲突时形成显式冲突记录。
* 公众只能读到脱敏安全摘要；执法人员可由消费凭证逆向导出责任主体、适用
  标准与证据链，并立即得到仍需封存的去向清单。
"""

from __future__ import annotations

import copy
import hashlib
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

EVENT_HARVEST = "harvest"        # 采收
EVENT_TEMPERATURE = "temperature"  # 运输温控
EVENT_REPACK = "repack"          # 分装（拆分/混装）
EVENT_INSPECTION = "inspection"  # 检验
EVENT_SALE = "sale"              # 销售
EVENT_RETURN = "return"          # 退回
EVENT_DISPOSAL = "disposal"      # 销毁
EVENT_SEIZURE = "seizure"        # 封存（执法措施）

TRACE_EVENT_TYPES = frozenset(
    {
        EVENT_HARVEST,
        EVENT_TEMPERATURE,
        EVENT_REPACK,
        EVENT_INSPECTION,
        EVENT_SALE,
        EVENT_RETURN,
        EVENT_DISPOSAL,
    }
)
# 可重复上报、按指纹合并的“事实类”事件；分装与封存是不可重复的关系/措施事件。
DEDUPLICABLE_TYPES = TRACE_EVENT_TYPES - {EVENT_REPACK}

# 包装形态
PACKAGING_CASE = "case"          # 箱
PACKAGING_PALLET = "pallet"      # 托盘
PACKAGING_BULK = "bulk"          # 散装批次

RESULT_PASS = "pass"
RESULT_FAIL = "fail"
CONDITION_OK = "ok"
CONDITION_BREACH = "breach"      # 温控超标

STATUS_DRAFT = "draft"
STATUS_CONFIRMED = "confirmed"
STATUS_SUPERSEDED = "superseded"  # 冲突裁决后被取代的版本
STATUS_RESOLVED = "resolved"      # 冲突经裁决后的定稿版本（与 confirmed 等效冻结）

# 角色
ROLE_ENTERPRISE = "enterprise"  # 企业
ROLE_AGRICULTURE = "agriculture"  # 农业农村部门
ROLE_MARKET = "market"          # 市场监管部门
ROLE_HEALTH = "health"          # 卫生部门
ROLE_PLATFORM = "platform"      # 平台
ROLE_INSPECTOR = "inspector"    # 执法人员
ROLE_PUBLIC = "public"          # 公众
# 分装/封存等措施事件的允许发起角色
MEASURE_ROLES = frozenset({ROLE_ENTERPRISE, ROLE_MARKET, ROLE_INSPECTOR})
CONFIRM_ROLES = frozenset(
    {ROLE_ENTERPRISE, ROLE_AGRICULTURE, ROLE_MARKET, ROLE_HEALTH, ROLE_PLATFORM, ROLE_INSPECTOR}
)

DISPOSITION_HELD = "held"
DISPOSITION_SOLD = "sold"
DISPOSITION_RETURNED = "returned"
DISPOSITION_DISPOSED = "disposed"
DISPOSITION_ACTIVE = frozenset({DISPOSITION_HELD, DISPOSITION_SOLD, DISPOSITION_RETURNED})


class TraceabilityError(Exception):
    """所有领域校验错误的基类，消息可直接面向调用方。"""


class NotFoundError(TraceabilityError):
    pass


class ValidationError(TraceabilityError):
    pass


class AuthorizationError(TraceabilityError):
    pass


class ConflictError(TraceabilityError):
    """离线回放与既有事实冲突时抛出，冲突已登记，等待显式处理。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _require(data, key, expected_type=None):
    if not isinstance(data, dict) or key not in data:
        raise ValidationError(f"缺少必填字段: {key}")
    value = data[key]
    if expected_type is not None and not isinstance(value, expected_type):
        raise ValidationError(f"字段 {key} 类型应为 {expected_type.__name__}")
    return value


def _public_lot_id(lot_id: str) -> str:
    digest = hashlib.sha256(lot_id.encode("utf-8")).hexdigest()[:10]
    return f"L-{digest}"


def _public_party(party_id: str) -> str:
    if not party_id:
        return party_id
    return f"主体***{hashlib.sha256(party_id.encode('utf-8')).hexdigest()[:6]}"


# ---------------------------------------------------------------------------
# 领域对象
# ---------------------------------------------------------------------------

class Lot:
    """追溯单元：箱、托盘或散装批次。"""

    def __init__(self, lot_id, packaging, product, quantity, unit, owner, created_at, extra=None):
        self.id = lot_id
        self.packaging = packaging
        self.product = product
        self.quantity = float(quantity)
        self.unit = unit
        self.owner = owner
        self.created_at = created_at
        self.extra = dict(extra or {})

    def to_dict(self):
        return {
            "lot": self.id,
            "packaging": self.packaging,
            "product": self.product,
            "quantity": self.quantity,
            "unit": self.unit,
            "owner": self.owner,
            "created_at": self.created_at,
            "extra": copy.deepcopy(self.extra),
        }


class TraceabilityService:
    """事件账本与追溯查询服务（内存实现，接口可直接映射到持久化存储）。"""

    def __init__(self, clock=now_iso):
        self._lock = threading.RLock()
        self._clock = clock
        self.lots = {}                       # lot_id -> Lot
        self.events = {}                     # event_id -> event dict
        self._event_index = []               # event_id 按账本顺序
        self.facts = defaultdict(dict)       # (type, subject, 发生日) -> fingerprint -> event_id
        self.standards = {}                  # standard_id -> dict
        self.recalls = {}                    # recall_id -> dict
        self.cases = {}                      # case_id -> 结案快照
        self.scan_records = {}               # scan_id -> scan dict
        self.consumers = {}                  # consumer_receipt 上链时登记的购买事件
        self._seq = 0

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _new_id(self, prefix):
        self._seq += 1
        return f"{prefix}_{uuid.uuid4().hex[:10]}{self._seq:04d}"

    def _tick(self, when):
        return when or self._clock()

    def _get_event(self, event_id):
        try:
            return self.events[event_id]
        except KeyError:
            raise NotFoundError(f"事件不存在: {event_id}")

    def _get_lot(self, lot_id):
        try:
            return self.lots[lot_id]
        except KeyError:
            raise NotFoundError(f"追溯单元不存在: {lot_id}")

    @staticmethod
    def _fingerprint(event_type, subject, payload):
        """同一事实的稳定指纹：忽略报送方、措辞与时间戳的细微差异。

        数值型测量值按两位小数归并，地点/结果/凭证等实质字段参与指纹。
        """

        def canonical(value):
            if isinstance(value, float):
                return round(value, 2)
            if isinstance(value, dict):
                return {k: canonical(value[k]) for k in sorted(value)}
            if isinstance(value, (list, tuple)):
                return [canonical(v) for v in value]
            return value

        material = {}
        for key in (
            "lot", "lots", "location", "result", "condition",
            "standard_id", "receipt", "reason_code", "vehicle",
            "destination", "consumer", "sample", "test_item",
            "farm", "producer", "lab", "method", "carrier", "channel",
            "reason", "buyer", "readings",
        ):
            if key in payload and payload[key] is not None:
                material[key] = canonical(payload[key])
        if "temperature" in payload:
            material["temperature"] = round(float(payload["temperature"]), 2)
        raw = f"{event_type}|{subject}|" + repr(canonical(material))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _conflict_key(payload):
        """冲突比对键：同一实质字段上的不同取值。"""
        keys = ("result", "condition", "temperature", "location", "destination", "reason_code", "readings")
        return {k: payload[k] for k in keys if k in payload}

    # ------------------------------------------------------------------
    # 追溯单元注册（采收事件自动建 lot，也允许预注册）
    # ------------------------------------------------------------------

    def register_lot(self, packaging, product, quantity, unit, owner, lot_id=None, extra=None, occurred_at=None):
        if packaging not in (PACKAGING_CASE, PACKAGING_PALLET, PACKAGING_BULK):
            raise ValidationError(f"未知包装形态: {packaging}")
        quantity = float(quantity)
        if quantity <= 0:
            raise ValidationError("数量必须为正数")
        with self._lock:
            lot_id = lot_id or self._new_id("L")
            if lot_id in self.lots:
                raise ValidationError(f"追溯单元已存在: {lot_id}")
            lot = Lot(lot_id, packaging, product, quantity, unit, owner, self._tick(occurred_at), extra)
            self.lots[lot_id] = lot
            return lot.to_dict()

    # ------------------------------------------------------------------
    # 事件上报
    # ------------------------------------------------------------------

    def submit_event(self, event_type, reporter, payload, occurred_at=None, reporter_ref=None):
        """上报一个事实事件（草稿）。

        重复上报同一事实时不新增事件，只把报送方合并进既有事件；
        若与既有同主题事实实质矛盾，则登记为该事件的冲突版本。
        返回 ``(event, deduped)``：deduped 为 True 表示命中既有事实。
        """
        if event_type not in TRACE_EVENT_TYPES:
            raise ValidationError(f"未知事件类型: {event_type}")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须为对象")
        subject = self._subject(event_type, payload)
        with self._lock:
            if event_type == EVENT_HARVEST:
                self._ensure_lot_from_harvest(payload, reporter)
            return self._submit_locked(event_type, reporter, payload, occurred_at, reporter_ref, subject)

    def _ensure_lot_from_harvest(self, payload, reporter):
        lot_id = _require(payload, "lot", str)
        if lot_id in self.lots:
            return
        packaging = payload.get("packaging", PACKAGING_BULK)
        if packaging not in (PACKAGING_CASE, PACKAGING_PALLET, PACKAGING_BULK):
            raise ValidationError(f"未知包装形态: {packaging}")
        quantity = float(_require(payload, "quantity"))
        if quantity <= 0:
            raise ValidationError("数量必须为正数")
        product = _require(payload, "product", str)
        unit = _require(payload, "unit", str)
        self.lots[lot_id] = Lot(
            lot_id, packaging, product, quantity, unit,
            payload.get("producer") or reporter.get("party"), self._clock(),
            payload.get("extra"),
        )

    @staticmethod
    def _subject(event_type, payload):
        if event_type == EVENT_REPACK:
            return "repack:" + ",".join(sorted(payload.get("inputs", []))) + "->" + ",".join(sorted(payload.get("outputs", [])))
        lot = payload.get("lot")
        if not lot:
            raise ValidationError("事件必须指定 lot")
        return lot

    def _submit_locked(self, event_type, reporter, payload, occurred_at, reporter_ref, subject):
        self._validate_payload(event_type, payload)
        timestamp = self._tick(occurred_at)
        fingerprint = self._fingerprint(event_type, subject, payload)
        # 同一事实按“同类型 + 同主体 + 同一发生日”归组：组内同指纹合并，
        # 组内实质取值不同登记为冲突；不同发生日是先后两次独立事实。
        day = _parse_ts(timestamp).date().isoformat()
        bucket = self.facts[(event_type, subject, day)] if event_type in DEDUPLICABLE_TYPES else None

        if bucket is not None and fingerprint in bucket:
            event = self.events[bucket[fingerprint]]
            self._merge_reporter(event, reporter, reporter_ref, timestamp)
            return event, True

        # 同组已有不同事实：成为冲突版本（草稿也参与冲突比对）
        existing_id = next(iter(bucket.values()), None) if bucket is not None else None
        event = {
            "event_id": self._new_id("E"),
            "type": event_type,
            "subject": subject,
            "lot": payload.get("lot"),
            "payload": copy.deepcopy(payload),
            "occurred_at": timestamp,
            "recorded_at": self._clock(),
            "status": STATUS_DRAFT,
            "reporters": [self._reporter_entry(reporter, reporter_ref, timestamp)],
            "versions": [],
            "fingerprint": fingerprint,
            "case_snapshots": [],
        }
        self.events[event["event_id"]] = event
        self._event_index.append(event["event_id"])
        bucket[fingerprint] = event["event_id"]
        if existing_id:
            self._register_conflict(self.events[existing_id], event)
        return event, False

    @staticmethod
    def _reporter_entry(reporter, ref, timestamp):
        return {"role": reporter.get("role"), "party": reporter.get("party"), "ref": ref, "at": timestamp}

    def _merge_reporter(self, event, reporter, ref, timestamp):
        entry = self._reporter_entry(reporter, ref, timestamp)
        for existing in event["reporters"]:
            if existing["role"] == entry["role"] and existing["party"] == entry["party"]:
                existing["ref"] = ref
                existing["at"] = timestamp
                return
        event["reporters"].append(entry)

    def _register_conflict(self, primary, challenger):
        pair = (self._conflict_key(primary["payload"]), self._conflict_key(challenger["payload"]))
        challenger["versions"].append({"conflicts_with": primary["event_id"], "pair": pair})
        primary["versions"].append({"conflicts_with": challenger["event_id"], "pair": pair})

    # ------------------------------------------------------------------
    # 分装：拆分与混装（保谱系的关键）
    # ------------------------------------------------------------------

    def repack(self, reporter, inputs, outputs, location=None, occurred_at=None, notes=None):
        """分装事件。

        inputs/outputs 为 ``[{"lot": ..., "quantity": ..., "consumed": ...}]``。
        * 拆分：一个输入 lot、多个输出 lot（箱拆零、托盘拆箱）；
        * 混装：多个输入 lot 合并到一个或多个输出 lot，输出按输入数量比例
          携带每个来源的份额，使散装批次仍保有全部来源关系。
        数量守恒：输入消耗量之和必须等于各输出数量之和。
        """
        with self._lock:
            if not inputs or not outputs:
                raise ValidationError("分装必须至少有一个输入和一个输出")
            input_total = 0.0
            normalized_inputs = []
            for item in inputs:
                lot = self._get_lot(_require(item, "lot", str))
                consumed = float(item.get("consumed") if item.get("consumed") is not None else item.get("quantity") or lot.quantity)
                if consumed <= 0:
                    raise ValidationError("分装投入量必须为正数")
                if consumed > lot.quantity + 1e-9:
                    raise ValidationError(f"{lot.id} 可用数量 {lot.quantity} 不足，无法投入 {consumed}")
                normalized_inputs.append({"lot": item["lot"], "consumed": consumed, "unit": lot.unit})
                input_total += consumed
            inputs = normalized_inputs
            output_total = 0.0
            normalized_outputs = []
            for item in outputs:
                out_lot_id = _require(item, "lot", str)
                quantity = float(_require(item, "quantity"))
                if out_lot_id in self.lots:
                    raise ValidationError(f"输出单元已存在: {out_lot_id}")
                if quantity <= 0:
                    raise ValidationError("分装产出量必须为正数")
                packaging = item.get("packaging", PACKAGING_CASE)
                product = item.get("product")
                normalized_outputs.append(
                    {"lot": out_lot_id, "quantity": quantity, "packaging": packaging, "product": product,
                     "extra": item.get("extra")}
                )
                output_total += quantity
            if abs(input_total - output_total) > 1e-6:
                raise ValidationError(f"数量不守恒：投入 {input_total} != 产出 {output_total}")

            # 比例来源：每个输出按输入消耗量比例携带来源份额
            shares = {item["lot"]: item["consumed"] / input_total for item in inputs}
            payload = {
                "inputs": [{"lot": i["lot"], "consumed": i["consumed"], "unit": i["unit"]} for i in inputs],
                "outputs": [{"lot": o["lot"], "quantity": o["quantity"]} for o in normalized_outputs],
                "shares": shares,
                "location": location,
                "notes": notes,
            }
            timestamp = self._tick(occurred_at)
            event = {
                "event_id": self._new_id("E"),
                "type": EVENT_REPACK,
                "subject": self._subject(EVENT_REPACK, {"inputs": [i["lot"] for i in inputs],
                                                        "outputs": [o["lot"] for o in normalized_outputs]}),
                "lot": normalized_outputs[0]["lot"],
                "payload": payload,
                "occurred_at": timestamp,
                "recorded_at": self._clock(),
                "status": STATUS_DRAFT,
                "reporters": [self._reporter_entry(reporter, None, timestamp)],
                "versions": [],
                "fingerprint": None,
                "case_snapshots": [],
            }
            # 落账成功后再扣减库存并建立输出单元
            for item in inputs:
                lot = self.lots[item["lot"]]
                lot.quantity = round(lot.quantity - item["consumed"], 9)
            first_input = self.lots[inputs[0]["lot"]]
            for out in normalized_outputs:
                product = out["product"] or first_input.product
                unit = first_input.unit
                self.lots[out["lot"]] = Lot(
                    out["lot"], out["packaging"], product, out["quantity"], unit,
                    reporter.get("party"), timestamp, out.get("extra"),
                )
            self.events[event["event_id"]] = event
            self._event_index.append(event["event_id"])
            return event

    # ------------------------------------------------------------------
    # 草稿修改 / 确认 / 冲突裁决
    # ------------------------------------------------------------------

    def revise_event(self, event_id, reporter, payload):
        """企业只可修改尚未确认的草稿。确认后任何修改都被拒绝。"""
        with self._lock:
            event = self._get_event(event_id)
            if event["status"] != STATUS_DRAFT:
                raise AuthorizationError(f"事件已{event['status']}，不可修改")
            if reporter.get("role") != ROLE_ENTERPRISE:
                raise AuthorizationError("仅企业可修订事件草稿")
            if event["reporters"][0]["party"] != reporter.get("party") and reporter.get("party") not in {
                r["party"] for r in event["reporters"]
            }:
                raise AuthorizationError("只能修订本企业上报的草稿")
            self._validate_payload(event["type"], payload)
            event["payload"] = copy.deepcopy(payload)
            event["revised_at"] = self._clock()
            return event

    def confirm_event(self, event_id, reporter):
        with self._lock:
            event = self._get_event(event_id)
            if reporter.get("role") not in CONFIRM_ROLES:
                raise AuthorizationError("该角色无权确认事件")
            if event["status"] != STATUS_DRAFT:
                raise ValidationError(f"事件状态为 {event['status']}，无需确认")
            open_conflicts = [v for v in event["versions"] if v.get("state") != STATUS_RESOLVED]
            if open_conflicts:
                raise ConflictError("存在未裁决的事实冲突，请先 resolve_conflict")
            event["status"] = STATUS_CONFIRMED
            event["confirmed_at"] = self._clock()
            event["confirmer"] = {"role": reporter.get("role"), "party": reporter.get("party")}
            return event

    def resolve_conflict(self, event_id, reporter, kept_payload, reason):
        """显式裁决冲突：定稿一个取值，矛盾版本标记为 superseded。

        案件结案后不允许借裁决之名倒改：被结案快照引用的事件需走新事件更正。
        """
        with self._lock:
            event = self._get_event(event_id)
            if reporter.get("role") not in (ROLE_MARKET, ROLE_INSPECTOR, ROLE_AGRICULTURE, ROLE_HEALTH):
                raise AuthorizationError("仅监管/执法角色可裁决事实冲突")
            if not event["versions"]:
                raise ValidationError("该事件没有待裁决冲突")
            if event["case_snapshots"]:
                raise AuthorizationError("事件已纳入结案判断，冲突裁决不得倒改，请以新事件更正")
            self._validate_payload(event["type"], kept_payload)
            # 矛盾版本落标
            related = [event] + [self.events[v["conflicts_with"]] for v in event["versions"]]
            for other in related:
                if other is event:
                    continue
                other["status"] = STATUS_SUPERSEDED
                other["superseded_at"] = self._clock()
                for v in other["versions"]:
                    v["state"] = STATUS_RESOLVED
            event["payload"] = copy.deepcopy(kept_payload)
            event["status"] = STATUS_CONFIRMED
            event["confirmed_at"] = self._clock()
            event["resolution"] = {"by": {"role": reporter.get("role"), "party": reporter.get("party")},
                                   "reason": reason, "at": self._clock()}
            for v in event["versions"]:
                v["state"] = STATUS_RESOLVED
                v["resolution"] = reason
            return event

    def pending_conflicts(self):
        with self._lock:
            return [
                {"event_id": eid, "type": e["type"], "subject": e["subject"],
                 "conflicts_with": [v["conflicts_with"] for v in e["versions"]
                                    if v.get("state") != STATUS_RESOLVED]}
                for eid, e in self.events.items()
                if any(v.get("state") != STATUS_RESOLVED for v in e["versions"])
            ]

    # ------------------------------------------------------------------
    # 门店失联：扫码缓存与恢复回放
    # ------------------------------------------------------------------

    def buffer_scan(self, scan_id, store, lot_id, scanned_at, recovered_at=None, payload=None):
        """门店失联期间缓存的扫码记录（箱/托盘码）。"""
        with self._lock:
            if scan_id in self.scan_records:
                raise ValidationError(f"扫码记录已存在: {scan_id}")
            record = {
                "scan_id": scan_id, "store": store, "lot": lot_id,
                "scanned_at": scanned_at, "recovered_at": recovered_at or self._clock(),
                "payload": payload or {}, "state": "buffered",
            }
            self.scan_records[scan_id] = record
            return record

    def replay_scan(self, scan_id, reporter):
        """通讯恢复后回放扫码。

        若账本显示该 lot 在扫码时点已销毁，则构成显式冲突（销毁后仍在售），
        冲突登记到销毁事件上并抛出 ConflictError，等待执法端显式处理。
        """
        with self._lock:
            scan = self.scan_records.get(scan_id)
            if scan is None:
                raise NotFoundError(f"扫码记录不存在: {scan_id}")
            if scan["state"] != "buffered":
                raise ValidationError(f"扫码记录状态为 {scan['state']}，不可重复回放")
            scanned_at = scan["scanned_at"]
            lot_id = scan["lot"]
            disposal = self._latest_confirmed(EVENT_DISPOSAL, lot_id, at_or_before=scanned_at)
            if disposal is not None:
                marker = {
                    "kind": "offline_scan_vs_disposal",
                    "scan_id": scan_id,
                    "lot": lot_id,
                    "store": scan["store"],
                    "scanned_at": scanned_at,
                    "disposal_event": disposal["event_id"],
                    "state": "open",
                }
                disposal["versions"].append(marker)
                scan["state"] = "conflict"
                scan["conflict"] = marker
                raise ConflictError(
                    f"扫码 {scan_id} 与销毁记录 {disposal['event_id']} 冲突：{lot_id} 在 {scanned_at} 仍出现于 {scan['store']}"
                )
            sale_payload = {"lot": lot_id, "location": scan["store"], "channel": "offline-scan",
                            **scan["payload"]}
            event, _ = self._submit_locked(
                EVENT_SALE, reporter, sale_payload, scanned_at, scan_id, lot_id
            )
            scan["state"] = "replayed"
            scan["event_id"] = event["event_id"]
            return event

    def resolve_scan_conflict(self, scan_id, reporter, action, note=None):
        """显式处理离线扫码与销毁记录的冲突。

        action:
        * ``product_located``：销毁后仍在售属实，对扫码所在单元发起封存核查
          （自动登记封存事件，单元从未决清单转入已封存）；
        * ``scan_error``：扫码为误扫/重复码，冲突销项。
        处理人与理由随冲突标记留痕，不删除任何原始记录。
        """
        with self._lock:
            scan = self.scan_records.get(scan_id)
            if scan is None:
                raise NotFoundError(f"扫码记录不存在: {scan_id}")
            marker = scan.get("conflict")
            if marker is None or marker.get("state") != "open":
                raise ValidationError("该扫码没有待处理的冲突")
            if reporter.get("role") not in (ROLE_MARKET, ROLE_INSPECTOR):
                raise AuthorizationError("仅市场监管/执法可处理扫码冲突")
            disposal = self.events[marker["disposal_event"]]
            if action == "product_located":
                seizure = self.seize(
                    marker["lot"], reporter,
                    f"离线扫码显示销毁后仍在 {marker['store']} 售出现身",
                    case_id=f"scan-{scan_id}", occurred_at=scan["recovered_at"],
                )
                marker["resolution_event"] = seizure["event_id"]
            elif action != "scan_error":
                raise ValidationError("action 必须为 product_located 或 scan_error")
            resolution = {"by": {"role": reporter.get("role"), "party": reporter.get("party")},
                          "action": action, "note": note, "at": self._clock()}
            marker["state"] = STATUS_RESOLVED
            marker["resolution"] = resolution
            for v in disposal["versions"]:
                if v.get("scan_id") == scan_id:
                    v["state"] = STATUS_RESOLVED
                    v["resolution"] = resolution
            scan["state"] = "resolved"
            return {"scan_id": scan_id, "action": action, "resolution": resolution,
                    "seizure_event": marker.get("resolution_event")}

    # ------------------------------------------------------------------
    # 标准：按生效时点适用
    # ------------------------------------------------------------------

    def publish_standard(self, standard_id, title, limits, effective_at, supersedes=None, reporter=None):
        """发布/更新限量标准。新标准生效不影响生效前已结案的判断。"""
        with self._lock:
            if standard_id in self.standards:
                raise ValidationError(f"标准已存在: {standard_id}")
            standard = {
                "standard_id": standard_id,
                "title": title,
                "limits": copy.deepcopy(limits),
                "effective_at": effective_at,
                "supersedes": supersedes,
                "published_at": self._clock(),
                "reporter": reporter,
            }
            self.standards[standard_id] = standard
            if supersedes:
                # 旧标准在新标准生效时点失效（双时点）
                prior = self.standards.get(supersedes)
                if prior is None:
                    raise ValidationError(f"被替代标准不存在: {supersedes}")
                prior["invalid_after"] = effective_at
            return standard

    def standard_at(self, at):
        """事件发生时点适用的标准（effective_at <= at < invalid_after）。"""
        moment = _parse_ts(at)
        candidates = []
        with self._lock:
            for standard in self.standards.values():
                effective = _parse_ts(standard["effective_at"])
                invalid_after = standard.get("invalid_after")
                if effective <= moment and (invalid_after is None or moment < _parse_ts(invalid_after)):
                    candidates.append(standard)
        candidates.sort(key=lambda s: _parse_ts(s["effective_at"]), reverse=True)
        return candidates[0] if candidates else None

    def evaluate_inspection(self, event_id):
        """按检验事件发生时点适用的标准判定合格/不合格。"""
        with self._lock:
            event = self._get_event(event_id)
            if event["type"] != EVENT_INSPECTION:
                raise ValidationError("仅检验事件可适用标准判定")
            standard = self.standard_at(event["occurred_at"])
            payload = event["payload"]
            if standard is None:
                return {"event_id": event_id, "verdict": "no_standard", "standard_id": None, "breaches": []}
            breaches = []
            readings = payload.get("readings", {})
            for item, limit in standard["limits"].items():
                if item in readings and float(readings[item]) > float(limit):
                    breaches.append({"item": item, "value": readings[item], "limit": limit})
            verdict = RESULT_FAIL if breaches else RESULT_PASS
            return {"event_id": event_id, "verdict": verdict,
                    "standard_id": standard["standard_id"], "standard_title": standard["title"],
                    "breaches": breaches, "evaluated_at": self._clock()}

    # ------------------------------------------------------------------
    # 召回：按生效时点圈定范围
    # ------------------------------------------------------------------

    def issue_recall(self, recall_id, reason, source_lots, effective_at, level="full", reporter=None):
        """发布召回。范围按生效时点的现状圈定：现存及已售出未销毁的去向，
        已销毁部分不在召回范围内。生效后不回溯改写此前的结案判断。
        """
        with self._lock:
            if recall_id in self.recalls:
                raise ValidationError(f"召回已存在: {recall_id}")
            effective = _parse_ts(effective_at)
            affected = set()
            for source in source_lots:
                affected.update(self._descendants_at(source, effective))
            # 生效时点前已销毁（且销毁在召回生效前）的单元排除
            scope, excluded = [], []
            for lot_id in sorted(affected):
                disposal = self._latest_confirmed(EVENT_DISPOSAL, lot_id, at_or_before=effective_at)
                entry = {"lot": lot_id, **self._whereabouts(lot_id, effective_at)}
                if disposal is not None:
                    entry["disposal_event"] = disposal["event_id"]
                    excluded.append(entry)
                else:
                    scope.append(entry)
            recall = {
                "recall_id": recall_id,
                "reason": reason,
                "source_lots": list(source_lots),
                "effective_at": effective_at,
                "level": level,
                "reporter": reporter,
                "scope": scope,
                "excluded_disposed": excluded,
                "issued_at": self._clock(),
            }
            self.recalls[recall_id] = recall
            return recall

    def get_recall(self, recall_id):
        with self._lock:
            try:
                return copy.deepcopy(self.recalls[recall_id])
            except KeyError:
                raise NotFoundError(f"召回不存在: {recall_id}")

    # ------------------------------------------------------------------
    # 封存
    # ------------------------------------------------------------------

    def seize(self, lot_id, reporter, reason, case_id, occurred_at=None):
        if reporter.get("role") not in (ROLE_MARKET, ROLE_INSPECTOR):
            raise AuthorizationError("仅市场监管/执法可执行封存")
        with self._lock:
            self._get_lot(lot_id)
            payload = {"lot": lot_id, "reason": reason, "case_id": case_id}
            event = {
                "event_id": self._new_id("E"),
                "type": EVENT_SEIZURE,
                "subject": lot_id,
                "lot": lot_id,
                "payload": payload,
                "occurred_at": self._tick(occurred_at),
                "recorded_at": self._clock(),
                "status": STATUS_CONFIRMED,
                "reporters": [self._reporter_entry(reporter, case_id, self._clock())],
                "versions": [],
                "fingerprint": None,
                "case_snapshots": [],
            }
            self.events[event["event_id"]] = event
            self._event_index.append(event["event_id"])
            return event

    # ------------------------------------------------------------------
    # 结案快照：判断不可倒改
    # ------------------------------------------------------------------

    def close_case(self, case_id, title, lot_ids, inspector, closed_at=None):
        """对一批单元作出结案判断并固化快照。

        快照记录时点、适用标准版本、检验结论与事件指纹；此后标准更替、
        事实修订或冲突裁决均不得改写本结论。
        """
        with self._lock:
            if case_id in self.cases:
                raise ValidationError(f"案件已结案: {case_id}")
            closed_at = closed_at or self._clock()
            related = []
            for eid in self._event_index:
                event = self.events[eid]
                if event["lot"] in lot_ids or any(
                    i["lot"] in lot_ids for i in event["payload"].get("inputs", [])
                ) or any(
                    o["lot"] in lot_ids for o in event["payload"].get("outputs", [])
                ):
                    related.append(event)
            snapshots = []
            for event in related:
                snap = {
                    "event_id": event["event_id"],
                    "type": event["type"],
                    "status_at_close": event["status"],
                    "payload": copy.deepcopy(event["payload"]),
                    "occurred_at": event["occurred_at"],
                    "fingerprint": event.get("fingerprint"),
                }
                if event["type"] == EVENT_INSPECTION:
                    snap["evaluation"] = self.evaluate_inspection(event["event_id"])
                snapshots.append(snap)
                event["case_snapshots"].append(case_id)
            case = {
                "case_id": case_id,
                "title": title,
                "lot_ids": sorted(lot_ids),
                "closed_at": closed_at,
                "inspector": {"role": inspector.get("role"), "party": inspector.get("party")},
                "events": snapshots,
            }
            self.cases[case_id] = case
            return copy.deepcopy(case)

    def get_case(self, case_id):
        with self._lock:
            try:
                return copy.deepcopy(self.cases[case_id])
            except KeyError:
                raise NotFoundError(f"案件不存在: {case_id}")

    # ------------------------------------------------------------------
    # 谱系查询
    # ------------------------------------------------------------------

    def _genealogy(self):
        parents = defaultdict(list)   # child -> [(parent, share)]
        children = defaultdict(list)  # parent -> [child]
        for eid in self._event_index:
            event = self.events[eid]
            if event["type"] != EVENT_REPACK:
                continue
            shares = event["payload"]["shares"]
            for out in event["payload"]["outputs"]:
                for inp in event["payload"]["inputs"]:
                    parents[out["lot"]].append((inp["lot"], shares.get(inp["lot"], 0.0)))
                    children[inp["lot"]].append(out["lot"])
        return parents, children

    def trace_origin(self, lot_id):
        """逆向溯源到最初采收单元，含比例（混装后各来源份额合计为 1）。"""
        with self._lock:
            self._get_lot(lot_id)
            parents, _ = self._genealogy()
            acc = defaultdict(float)
            acc[lot_id] = 1.0
            queue = deque([lot_id])
            while queue:
                current = queue.popleft()
                for parent, share in parents.get(current, []):
                    weight = acc[current] * share
                    acc[parent] += weight
                    queue.append(parent)
            roots, chain = [], []
            for node, weight in acc.items():
                event = self._latest_confirmed_or_any(EVENT_HARVEST, node)
                if event is not None:
                    roots.append({
                        "lot": node,
                        "share": round(weight, 6),
                        "harvest_event": event["event_id"],
                        "farm": event["payload"].get("farm"),
                        "producer": event["payload"].get("producer"),
                        "occurred_at": event["occurred_at"],
                    })
                if parents.get(node):
                    for parent, share in parents[node]:
                        chain.append({"child": node, "parent": parent, "share": round(share, 6)})
            return {"lot": lot_id, "roots": sorted(roots, key=lambda r: -r["share"]),
                    "links": chain}

    def trace_destinations(self, lot_id):
        """正向追踪：列出所有派生单元及其当前去向（供封存清单使用）。"""
        with self._lock:
            self._get_lot(lot_id)
            _, children = self._genealogy()
            result = []
            seen = set()
            queue = deque([lot_id])
            while queue:
                current = queue.popleft()
                if current in seen:
                    continue
                seen.add(current)
                result.append({"lot": current, **self._whereabouts(current, self._clock())})
                for child in children.get(current, []):
                    queue.append(child)
            return result

    def _descendants_at(self, lot_id, at):
        _, children = self._genealogy()
        seen = {lot_id}
        queue = deque([lot_id])
        while queue:
            current = queue.popleft()
            for child in children.get(current, []):
                repack = self._producing_repack(child)
                if repack is not None and _parse_ts(repack["occurred_at"]) <= _parse_ts(at) and child not in seen:
                    seen.add(child)
                    queue.append(child)
        return seen

    def _producing_repack(self, lot_id):
        for eid in reversed(self._event_index):
            event = self.events[eid]
            if event["type"] == EVENT_REPACK and any(o["lot"] == lot_id for o in event["payload"]["outputs"]):
                return event
        return None

    # ------------------------------------------------------------------
    # 去向与状态
    # ------------------------------------------------------------------

    def _latest_confirmed(self, event_type, lot_id, at_or_before=None, confirmed_only=True):
        """取该单元最近一个有效事件。

        confirmed_only=True 时仅承认已确认（或经裁决定稿）事件的法律效果，
        用于召回排除、离线扫码冲突等判断；运营性去向查询可纳入草稿。
        """
        latest = None
        for eid in self._event_index:
            event = self.events[eid]
            if event["type"] != event_type or event.get("lot") != lot_id:
                continue
            if event["status"] == STATUS_SUPERSEDED:
                continue
            if confirmed_only and event["status"] not in (STATUS_CONFIRMED, STATUS_RESOLVED):
                continue
            if at_or_before is not None and _parse_ts(event["occurred_at"]) > _parse_ts(at_or_before):
                continue
            latest = event
        return latest

    def _latest_confirmed_or_any(self, event_type, lot_id):
        confirmed = self._latest_confirmed(event_type, lot_id)
        if confirmed is not None:
            return confirmed
        for eid in reversed(self._event_index):
            event = self.events[eid]
            if event["type"] == event_type and event.get("lot") == lot_id and event["status"] != STATUS_SUPERSEDED:
                return event
        return None

    def _whereabouts(self, lot_id, at):
        """某单元在时点 at 的状态、所在/流向与责任主体。"""
        at = _parse_ts(at)
        latest = {"disposition": DISPOSITION_HELD, "event_id": None, "at": None}
        party = self.lots[lot_id].owner
        location = None
        for eid in self._event_index:
            event = self.events[eid]
            if event.get("lot") != lot_id or event["type"] == EVENT_SEIZURE:
                continue
            if event["status"] == STATUS_SUPERSEDED:
                continue
            if _parse_ts(event["occurred_at"]) > at:
                continue
            payload = event["payload"]
            if event["type"] == EVENT_SALE:
                latest = {"disposition": DISPOSITION_SOLD, "event_id": event["event_id"],
                          "at": event["occurred_at"]}
                location = payload.get("location")
                party = payload.get("buyer") or payload.get("location") or party
            elif event["type"] == EVENT_RETURN:
                latest = {"disposition": DISPOSITION_RETURNED, "event_id": event["event_id"],
                          "at": event["occurred_at"]}
                party = payload.get("received_by") or party
                location = payload.get("location", location)
            elif event["type"] == EVENT_DISPOSAL:
                latest = {"disposition": DISPOSITION_DISPOSED, "event_id": event["event_id"],
                          "at": event["occurred_at"]}
                party = payload.get("operator") or party
                location = payload.get("location", location)
            elif event["type"] == EVENT_TEMPERATURE:
                location = payload.get("destination", location)
                party = payload.get("carrier") or party
            elif event["type"] == EVENT_REPACK:
                location = payload.get("location", location)
        seized = None
        for eid in self._event_index:
            event = self.events[eid]
            if event["type"] == EVENT_SEIZURE and event.get("lot") == lot_id and _parse_ts(event["occurred_at"]) <= at:
                seized = {"event_id": event["event_id"], "reason": event["payload"]["reason"],
                          "case_id": event["payload"]["case_id"], "at": event["occurred_at"]}
        return {**latest, "party": party, "location": location, "seized": seized}

    # ------------------------------------------------------------------
    # 销售凭证（公众/执法入口）
    # ------------------------------------------------------------------

    def register_consumer_sale(self, receipt, lot_ids, store, sold_at, consumer=None, reporter=None):
        """登记消费凭证与购买 lot 的对应，供凭小票逆向追溯。"""
        with self._lock:
            if receipt in self.consumers:
                raise ValidationError(f"消费凭证已存在: {receipt}")
            for lot_id in lot_ids:
                self._get_lot(lot_id)
            record = {
                "receipt": receipt,
                "lots": list(lot_ids),
                "store": store,
                "sold_at": sold_at,
                "consumer": consumer,
                "reporter": reporter,
                "recorded_at": self._clock(),
            }
            self.consumers[receipt] = record
            return record

    def investigate_receipt(self, receipt, inspector):
        """执法人员由一份消费凭证逆向导出：责任主体、适用标准、证据链，
        以及仍需封存的去向清单。"""
        if inspector.get("role") != ROLE_INSPECTOR:
            raise AuthorizationError("仅执法人员可发起凭证逆向调查")
        with self._lock:
            sale = self.consumers.get(receipt)
            if sale is None:
                raise NotFoundError(f"消费凭证不存在: {receipt}")
            lots, parties, evidence, standards_used = set(), set(), [], {}
            evidence_ids = set()
            for lot_id in sale["lots"]:
                origin = self.trace_origin(lot_id)
                lots.add(lot_id)
                for root in origin["roots"]:
                    parties.add(root.get("producer"))
                for eid in self._evidence_events(lot_id):
                    evidence_ids.add(eid)
            # 责任主体与适用标准均沿完整证据链（含谱系上游）提取
            for eid in evidence_ids:
                event = self.events[eid]
                entry = self._evidence_entry(event)
                evidence.append(entry)
                if event["type"] == EVENT_INSPECTION:
                    evaluation = entry["evaluation"]
                    if evaluation.get("standard_id"):
                        standards_used[evaluation["standard_id"]] = evaluation["standard_title"]
                for rep in event["reporters"]:
                    if rep.get("party"):
                        parties.add(rep["party"])
                payload = event["payload"]
                for key in ("producer", "carrier", "operator", "repacker", "buyer", "received_by"):
                    if payload.get(key):
                        parties.add(payload[key])
            # 消费凭证登记方（如平台）本身也是责任环节
            if sale.get("reporter") and sale["reporter"].get("party"):
                parties.add(sale["reporter"]["party"])
            # 去重证据
            dedup = {e["event_id"]: e for e in evidence}
            evidence_sorted = sorted(dedup.values(), key=lambda e: e["occurred_at"])
            to_seize = self.still_to_seize(sale["lots"])
            return {
                "receipt": receipt,
                "store": sale["store"],
                "sold_at": sale["sold_at"],
                "lots": sorted(lots),
                "responsible_parties": sorted(p for p in parties if p),
                "applicable_standards": [{"standard_id": k, "title": v} for k, v in standards_used.items()],
                "evidence_chain": evidence_sorted,
                "still_to_seize": to_seize,
            }

    def _evidence_events(self, lot_id):
        """证据链：本 lot 自身事件 + 谱系链路上的事件（按时间）。"""
        parents, children = self._genealogy()
        related = {lot_id}
        queue = deque([lot_id])
        while queue:
            cur = queue.popleft()
            for parent, _ in parents.get(cur, []):
                if parent not in related:
                    related.add(parent)
                    queue.append(parent)
        ids = []
        for eid in self._event_index:
            event = self.events[eid]
            if event.get("lot") in related:
                ids.append(eid)
            elif event["type"] == EVENT_REPACK and any(i["lot"] in related for i in event["payload"]["inputs"]):
                ids.append(eid)
        return ids

    def _evidence_entry(self, event):
        entry = {
            "event_id": event["event_id"],
            "type": event["type"],
            "lot": event.get("lot"),
            "occurred_at": event["occurred_at"],
            "status": event["status"],
            "reporters": [{"role": r["role"], "party": r["party"]} for r in event["reporters"]],
            "payload": copy.deepcopy(event["payload"]),
        }
        if event["type"] == EVENT_INSPECTION:
            entry["evaluation"] = self.evaluate_inspection(event["event_id"])
        return entry

    def still_to_seize(self, lot_ids):
        """立即列出仍需封存的去向：现存/已售出/退回但未销毁、未封存的单元。"""
        with self._lock:
            result = []
            for source in lot_ids:
                for row in self.trace_destinations(source):
                    # 只有已确认的销毁才能免除封存义务，草稿销毁不算
                    if row["disposition"] == DISPOSITION_DISPOSED and self._latest_confirmed(
                        EVENT_DISPOSAL, row["lot"]
                    ) is not None:
                        continue
                    if row.get("seized"):
                        continue
                    result.append(row)
            # 同一 lot 可能由多个 source 派生，按 lot 去重
            unique = {row["lot"]: row for row in result}
            return sorted(unique.values(), key=lambda r: r["lot"])

    # ------------------------------------------------------------------
    # 公众安全摘要（脱敏）
    # ------------------------------------------------------------------

    def public_summary(self, lot_id):
        with self._lock:
            lot = self._get_lot(lot_id)
            inspections, disposals, recalls = [], [], []
            for eid in self._event_index:
                event = self.events[eid]
                if event.get("lot") != lot_id or event["status"] == STATUS_SUPERSEDED:
                    continue
                if event["type"] == EVENT_INSPECTION:
                    evaluation = self.evaluate_inspection(eid)
                    inspections.append({"at": event["occurred_at"], "verdict": evaluation["verdict"],
                                        "standard_id": evaluation.get("standard_id")})
                elif event["type"] == EVENT_DISPOSAL:
                    disposals.append({"at": event["occurred_at"], "reason_code": event["payload"].get("reason_code")})
            for recall in self.recalls.values():
                if _parse_ts(recall["effective_at"]) <= _parse_ts(self._clock()):
                    if any(s["lot"] == lot_id for s in recall["scope"]):
                        recalls.append({"recall_id": recall["recall_id"], "reason": recall["reason"],
                                        "effective_at": recall["effective_at"]})
            return {
                "lot_ref": _public_lot_id(lot_id),
                "product": lot.product,
                "safety": "unsafe" if any(i["verdict"] == RESULT_FAIL for i in inspections) or recalls
                else ("checked" if inspections else "unverified"),
                "inspections": inspections,
                "recalls": recalls,
                "disposed": bool(disposals),
                "owner": None,  # 不向公众暴露责任主体身份
            }

    # ------------------------------------------------------------------
    # 只读访问
    # ------------------------------------------------------------------

    def get_event(self, event_id):
        with self._lock:
            return copy.deepcopy(self._get_event(event_id))

    def list_events(self, event_type=None, lot_id=None):
        with self._lock:
            result = []
            for eid in self._event_index:
                event = self.events[eid]
                if lot_id is not None and event.get("lot") != lot_id and lot_id not in {
                    i["lot"] for i in event["payload"].get("inputs", [])
                } and lot_id not in {o["lot"] for o in event["payload"].get("outputs", [])}:
                    continue
                if event_type is not None and event["type"] != event_type:
                    continue
                result.append(copy.deepcopy(event))
            return result

    def get_lot(self, lot_id):
        with self._lock:
            return self._get_lot(lot_id).to_dict()

    # ------------------------------------------------------------------
    # payload 校验
    # ------------------------------------------------------------------

    def _validate_payload(self, event_type, payload):
        lot = payload.get("lot")
        if lot is not None and lot not in self.lots and event_type != EVENT_HARVEST:
            raise NotFoundError(f"追溯单元不存在: {lot}")
        if event_type == EVENT_HARVEST:
            for key in ("farm", "producer"):
                _require(payload, key, str)
        elif event_type == EVENT_TEMPERATURE:
            _require(payload, "temperature")
            float(payload["temperature"])
            _require(payload, "condition", str)
            if payload["condition"] not in (CONDITION_OK, CONDITION_BREACH):
                raise ValidationError("condition 必须为 ok 或 breach")
        elif event_type == EVENT_INSPECTION:
            _require(payload, "lab", str)
            readings = payload.get("readings")
            if readings is not None and not isinstance(readings, dict):
                raise ValidationError("readings 必须为检测项到数值的映射")
        elif event_type == EVENT_SALE:
            _require(payload, "location", str)
        elif event_type == EVENT_RETURN:
            _require(payload, "reason", str)
        elif event_type == EVENT_DISPOSAL:
            _require(payload, "method", str)
            _require(payload, "operator", str)
        return True
