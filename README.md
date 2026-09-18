# 食品全链条追溯

服务于从产地到餐桌的食品追溯。箱、托盘和散装批次在拆分、混装后仍保有来源
关系；采收、运输温控、分装、检验、销售、退回、销毁和门店扫码事件形成一条可
逆向核对的证据链。

## 解决的问题

- **单据互不相认**：农业、市场监管、卫生部门和平台各自的许可、检测、销毁
  单据统一挂到责任主体与事件上；重复上报同一事实（同一报告号/销毁单号）
  只形成一条事件记录，多方上报作为佐证附在同一条记录上。口径不一致时标记
  为 `disputed`，由执法方裁定，任何一方都不能静默覆盖。
- **拆分混装丢来源**：分装事件以 `inputs`/`outputs` 建账，混装产出通过
  `sources` 声明各自的来源投入量（须账量守恒），谱系可逐跳上溯或下查。
- **草稿与结案被倒改**：企业只能修改、确认自己的草稿；确认后只可追加不可
  改写。检验确认时冻结当时适用的**标准版本快照**，新标准只对生效后的判断
  有效；结案记录冻结结论与标准快照。
- **召回范围与时点**：召回按决定生效时点圈定范围（生效前已售出的数量不进
  封存清单），范围集合在决定时固化；`pending_seals` 随封存/销毁动态收敛，
  立刻回答“还需要封哪里、封多少”。
- **门店失联**：离线扫码在恢复联网后批量回补（`/scans/sync`），干净记录
  直接入账，与既有链矛盾的（已销毁又出现、位置不符、召回后仍卖出、批次不
  存在）生成显式冲突单，必须由执法方处理，不静默丢弃或覆盖。
- **角色可见性**：企业只能动自己的草稿；公众只能看到脱敏安全摘要（风险
  等级、处置指引、时间线）；执法方可凭消费凭证逆向导出责任主体、适用标准、
  完整证据链与仍需封存的去向。

## 运行

```bash
python3 service.py --check            # 基础配置与领域冒烟检查
python3 service.py --port 8000        # 启动服务
curl http://localhost:8000/health     # 服务身份
npm test                              # 领域场景 + HTTP 契约测试
```

## 主要接口

企业侧（`X-Token` 为登记主体时返回的令牌）：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /parties` | 登记责任主体，返回 id 与令牌 |
| `POST /parties/{id}/documents` | 登记许可/检测/销毁单据 |
| `POST /events?type=<harvest\|transport\|repack\|inspection\|transfer\|sale\|return\|destruction>` | 上报事件（企业为草稿，监管部门提交即正式）；`X-Op-Id` 支持断网重试幂等 |
| `POST /events/{id}/edit` | 修改自己未确认的草稿 |
| `POST /events/{id}/confirm` | 确认草稿（检验事件冻结标准快照） |
| `POST /scans/sync` | 离线扫码批量回补，返回 accepted 与 conflicts |

执法侧（主体类型须为 `authority`）：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /standards` | 发布标准版本（按时点选用） |
| `POST /events/{id}/resolve-dispute` | 裁定同一事实上的口径分歧 |
| `GET /conflicts?status=open` | 列出待处理的扫码冲突 |
| `POST /conflicts/{id}/resolve` | 处理冲突（accept_scan/reject_scan/confirm_existing） |
| `POST /recalls` | 发起召回（按生效时点圈范围，返回 pending_seals） |
| `POST /recalls/{id}/seals` | 登记封存，核减待封存清单 |
| `POST /cases` / `GET /cases/{id}` | 结案（冻结结论与标准快照） |
| `GET /trace/receipts/{receiptNo}` | 凭消费凭证逆向导出全链 |

任意已登录主体可查 `GET /batches/{id}/lineage?direction=up|down|both`。

公众侧（无需令牌，仅脱敏信息）：

- `GET /public/batches/{id}` / `GET /public/receipts/{no}`：风险等级、
  处置指引与时间线；批次号以哈希摘要展示。
- `GET /public/recalls/{id}`：召回产品、原因与退货指引，不含批次去向明细。

## 代码结构

- `traceability.py`：领域核心（存储、谱系、事实归并、标准时点、召回封存、
  离线冲突、逆向证据链、脱敏摘要），不依赖第三方库。
- `service.py`：HTTP 适配层（鉴权、路由、JSON 编解码）与健康检查。
- `test_traceability.py`：16 个端到端领域场景。
- `service_contract.py`：健康检查与按角色的 HTTP 契约测试。
