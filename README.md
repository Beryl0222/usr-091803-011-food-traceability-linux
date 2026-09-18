# 食品全链条追溯

服务于产地到餐桌的食品追溯：让箱、托盘和散装批次在拆分、混装后仍保有来源
关系；接收采收、运输温控、分装、检验、销售、退回和销毁事件；在跨部门重复
上报时只形成一条可核对记录；并为执法提供从消费凭证到责任主体与封存清单的
逆向导出能力。

仅依赖 Python 标准库，`service.py` 提供 HTTP 服务，`traceability.py` 是
线程安全的领域核心，可直接嵌入或替换持久化后端。

## 运行

```bash
python3 service.py --check          # 基础配置 + 领域冒烟检查
python3 service.py --port 8000      # 启动后访问 /health
npm test                            # 运行全部契约与领域测试（28 项）
```

## 领域规则

### 追溯单元与谱系

* `Lot` 有三种包装形态：`case`（箱）、`pallet`（托盘）、`bulk`（散装批次）。
* 分装事件（拆分/混装）要求**数量守恒**；混装输出按各输入消耗量的比例携带
  来源份额，因此任意下游单元都能逆向算出每个产地批次的占比。
* `GET /lots/{lot}/origin` 逆向溯源到采收单元（含比例、农场、生产者）；
  `GET /lots/{lot}/destinations` 正向追踪全部派生去向。

### 事件与去重

* 七类事实事件：`harvest`、`temperature`、`repack`、`inspection`、`sale`、
  `return`、`disposal`，外加执法措施 `seizure`（封存）。
* 同一事实按「类型 + 主体 + 发生日 + 实质字段指纹」归并：农业、市场监管、
  卫生、平台重复上报只合并报送方，返回 `200 {"deduped": true}`，不产生新
  记录；首次为 `201`。
* 同组实质取值矛盾（如一组合格、一组不合格）不会互相覆盖，而是登记为
  **冲突版本**：`GET /conflicts` 可见，带冲突的事件不能确认；必须由监管/
  执法角色调用 `resolve-conflict` 显式裁决，败方版本标记 `superseded`。

### 草稿、确认与结案不可变

* 事件先落 `draft`，仅**本企业**可修订；`confirm` 后冻结，任何人不可修改。
* `POST /cases` 结案时固化证据快照（含当时适用标准与检验结论）。此后标准
  更替或冲突裁决都不得倒改快照；被结案引用的事件只能以**新事件**更正。

### 标准与召回的双时点

* 标准带 `effective_at`，新标准可通过 `supersedes` 让旧标准在生效时点失效。
  检验结论按**检验发生时点**适用的标准判定，历史结论不因新标准而改变。
* 召回按 `effective_at` 的现状圈定范围：现存与已售出但未销毁的去向在列；
  生效时点前**已确认销毁**的单元排除并留痕（`excluded_disposed`）。

### 门店失联与扫码冲突

* 失联期间扫码先 `POST /scans` 缓存；恢复后 `POST /scans/{id}/replay`。
* 若扫码时点该批次已有**确认销毁**记录，回放不生成销售，而是返回 `409`
  并在销毁事件上挂起显式冲突。监管/执法用 `POST /scans/{id}/resolve`
  处理：`product_located`（销毁后仍在售，自动封存）或 `scan_error`（误扫
  销项），原始记录全部保留。

### 角色与可见性

| 角色 (`X-Role`) | 主要能力 |
| --- | --- |
| `enterprise` | 上报事件、分装、修订本企业草稿、确认 |
| `agriculture` / `market` / `health` | 上报、确认；监管角色可裁决冲突 |
| `platform` | 上报销售/凭证、回放离线扫码 |
| `inspector` | 封存、裁决、结案、凭证逆向调查 |
| 公众（无头） | 仅 `GET /public/lots/{lot}/summary` |

机构中文名称如无法放进 HTTP 头，可放在请求体 `"party"` 字段。

公众摘要对批次码与责任主体做哈希脱敏，只给产品、检验结论（`checked` /
`unsafe` / `unverified`）、召回与是否已销毁。

### 执法逆向调查

`GET /receipts/{receipt}/investigation`（需 `X-Role: inspector`）返回：

* `responsible_parties`：产地生产者、冷链承运方、分装企业、销售门店/平台；
* `applicable_standards`：证据链中各检验按发生时点适用的标准；
* `evidence_chain`：采收 → 温控 → 分装 → 检验 → 销售的完整有序证据；
* `still_to_seize`：仍需封存的去向（未确认销毁、未封存的现存/已售/退回
  单元）；执行 `POST /seizures` 后即从清单移除。

## API 速览

```
POST /lots                                  预注册追溯单元
POST /events                                上报事实事件（自动去重/挂冲突）
POST /repack                                分装（拆分/混装，数量守恒）
POST /events/{id}/revise                    企业修订草稿
POST /events/{id}/confirm                   确认冻结
POST /events/{id}/resolve-conflict          裁决事实冲突
GET  /events[?type=&lot=]  /events/{id}     事件查询
GET  /lots/{id}/origin | /destinations      谱系查询
POST /standards                             发布/更替限量标准
POST /recalls                               按生效时点发布召回
POST /scans | /scans/{id}/replay|resolve    离线扫码缓存、回放、冲突处理
POST /seizures                              封存
POST /cases | GET /cases/{id}               结案与快照
POST /receipts                              登记消费凭证
GET  /receipts/{id}/investigation           执法逆向调查
GET  /seizure-list?lots=a,b                 仍需封存清单
GET  /public/lots/{id}/summary              公众脱敏摘要
GET  /conflicts                             未决事实冲突
```

错误以结构化 JSON 返回：`400 validation_error`、`403 forbidden`、
`404 not_found`、`409 conflict`、`422 domain_error`。

## 边界与后续

当前账本为进程内内存实现（重启清空），所有领域方法均为小粒度纯接口，
持久化时把 `events/lots/standards/recalls/cases` 落到只增表即可，规则层
无需改动；生产部署还需在边界补充调用方认证（当前仅做角色判定）。
