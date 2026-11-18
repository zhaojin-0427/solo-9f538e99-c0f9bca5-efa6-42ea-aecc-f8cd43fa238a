# PM2.5 修订采集与查询平台

市环境监测中心会补正各站点的小时 PM2.5,分析作业需按原版复算。本平台包含:

- **source**(模拟源站,FastAPI):分页发布修订。首版(v1)含全量站点,此后每版只列
  新增、改值(`upsert`)和删除(`delete`),响应带连续版号 `version`、页号 `page`、
  总页数 `total_pages`。发布修订时可附**原因编号 `reason_code`**(修订级默认,
  单条 op 可携带自身原因覆盖),每个分页响应的信封与条目都会带出原因。
  管理接口可发布/切换修订、注入缺页、超时、同版同站冲突、各页原因不一致故障。
- **collector**(采集与查询 API,FastAPI + SQLite):采集某 UTC 小时前先固定源站当前版,
  再逐版补齐;每个确认版的完整快照均可查询;采集时把**源站实际发布的原始操作**
  (upsert/delete,含同值 upsert)、操作前后值、原因编号与确认读数在同一事务保存;
  数据与当前版指针在同一事务生效。

## 快速开始

```bash
docker compose up --build
```

- 采集/查询 API: http://localhost:8000 (文档 `/docs`)
- 模拟源站: http://localhost:8001 (文档 `/docs`)

端到端冒烟(55+ 项断言,覆盖缺页/超时/冲突/原因不一致/同值 upsert/轨迹/回退):

```bash
python3 scripts/smoke.py
```

旧库升级专项(已确认版无原始操作时必须返回"来源未留存",禁止按快照差额补造):

```bash
python3 scripts/legacy_check.py
```

重启持久化专项(真实进程杀掉重启,读数与轨迹仍可查):

```bash
python3 scripts/restart_check.py
```

## 典型流程

```bash
# 1. 采集某 UTC 小时(先固定源站当前版,再逐版补齐)
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 2. 源站发布新修订(可带原因编号;也可带 {"ops":[...]} 显式指定,单条 op 可覆盖原因)
curl -X POST localhost:8001/admin/publish -H 'Content-Type: application/json' \
     -d '{"reason_code": 77, "ops": [
       {"station_id": "S01", "op": "upsert", "pm25": 42.0},
       {"station_id": "S02", "op": "upsert", "pm25": 50.0, "reason_code": 88},
       {"station_id": "S03", "op": "delete"}]}'

# 3. 再次采集,逐版补齐到新固定版
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 4. 查询:当前版(默认)或任一已确认版当时的完整数据
curl localhost:8000/hours/2026-11-18T10/data
curl 'localhost:8000/hours/2026-11-18T10/data?version=1'
curl 'localhost:8000/hours/2026-11-18T10/data?version=3&station_id=S07'
curl localhost:8000/hours/2026-11-18T10/versions

# 5. 按 UTC 小时 + 站点查修订轨迹(原始操作、前后值、原因、来源页;含同值 upsert)
curl localhost:8000/hours/2026-11-18T10/stations/S01/trajectory
```

## 故障注入演示(缺页 / 超时 / 同版同站冲突 / 各页原因不一致)

```bash
# 下一版第 1 页:超时 1 次(重试可成功,且不混入上次残页)
curl -X PUT localhost:8001/admin/faults -H 'Content-Type: application/json' -d \
  '{"faults":[{"version":2,"page":1,"kind":"timeout","times":1}]}'

# kind 还可取 missing(缺页 404)、conflict(同版同站不同值)、
# reason_mismatch(篡改该页信封原因,制造各页原因不一致;需多页版本才能被采集端发现);
# times=-1 表示持续生效
curl -X DELETE localhost:8001/admin/faults   # 清除全部故障
```

采集端对每版最多重试 `MAX_ATTEMPTS` 次;仍失败则**保留原供数版**(当前版指针不动),
该小时状态记为 `failed`,**该版读数与轨迹都不确认**;清除故障后再次采集即可继续补齐。

## 原始操作轨迹与"来源未留存"

- 每确认一版,源站实际发布的每条操作都原样写入 `raw_ops`(与快照同事务):
  操作类型、读数、**操作前值/操作后值**、原因编号、来源页号与版内顺序。
  **同值 upsert 也会如实留存**,不会因为快照值没变而被吞掉。
- 轨迹接口 `GET /hours/{hour}/stations/{station_id}/trajectory` 只返回 `raw_ops`
  里的真实操作。升级前已确认、但当时没有留存原始操作的版本,在
  `unretained_versions` 中逐版标注 `"notice": "来源未留存"`,**严禁根据快照差额
  反向补造** upsert/delete。
- 缺页、抓取失败、同版同站冲突、各页原因不一致的版本根本不会确认,因此既查不到
  读数也查不到轨迹;重试期间的残页只存内存、不落库。

## 需求映射

| 需求 | 实现 |
| --- | --- |
| 先固定源站当前版,再逐版补齐 | `collector/app/service.py::_collect`:开头一次 `GET /api/revisions/current` 固定目标,之后 `prev+1..pinned` 逐版抓取 |
| 源站发布修订可附原因编号,分页响应逐页带出 | `source/app/main.py`:`PublishRequest.reason_code`(修订级)与 `Op.reason_code`(单条覆盖);页信封与条目均返回 `reason_code` |
| 保存原始 upsert/delete、前后值、原因与确认读数 | `raw_ops` 表:同事务写入 op/pm25/before_pm25/after_pm25/reason_code/page/seq;`entries` 保存确认快照;`service.py::_build_raw_rows` 按实际操作顺序推导前后值 |
| 同值 upsert 必须可查 | 同值 upsert 不被快照吞掉,`raw_ops` 原样留存(before==after),轨迹接口可见 |
| 按 UTC 小时、站点查修订轨迹 | `GET /hours/{hour}/stations/{station_id}/trajectory` |
| 无原始操作记录的已确认版明确返回"来源未留存",不按快照差额补造 | 轨迹接口对整版无 `raw_ops` 行的确认版输出 `unretained_versions` + 固定文案;`scripts/legacy_check.py` 专项验证 |
| 缺页/抓取失败/各页原因不一致时读数与轨迹都不确认 | `MissingPage`/`PageTimeout`/`VersionConflict`/`ReasonMismatch` 均整版重试,失败则不提交事务,指针不动、残页不落库 |
| 重试不能混入残页 | 页面只存内存、从不落库;每次重试从第 1 页整版重取(`_fetch_version`) |
| 成功版重启后仍可查,原有读数查询照常 | SQLite WAL + 数据卷持久化;`restart_check.py` 杀进程重启验证;`/hours/{hour}/data` 口径不变 |
| 已确认版均可查当时完整数据 | 每版确认时物化全量快照到 `entries`,`GET /hours/{hour}/data?version=N` |
| 数据与当前版指针一同生效 | 单事务写入 `versions` + `raw_ops` + `entries` + `hours.current_version`(`db.py::tx`) |
| 失败或重启不得暴露半版 | 事务原子提交;启动时仅复位 `collecting` 状态元信息 |

## API 一览

**collector(8000)**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/collections` | 采集某 UTC 小时,body `{"hour":"YYYY-MM-DDTHH"}`;采集中返回 409 |
| GET | `/collections/{hour}` | 该小时采集状态/最近错误 |
| GET | `/hours` | 全部小时及当前版指针 |
| GET | `/hours/{hour}/versions` | 已确认版列表(含 `raw_ops_retained` 留存标记) |
| GET | `/hours/{hour}/data` | 某版完整数据;`version` 缺省取当前版,`station_id` 可单站过滤 |
| GET | `/hours/{hour}/stations/{station_id}/trajectory` | 该站修订轨迹:真实原始事件(含前后值/原因/来源页)+ 未留存版本声明 |

**source(8001)**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/revisions/current` | 当前版号 |
| GET | `/api/revisions` | 全部修订元信息(含修订级 `reason_code`) |
| GET | `/api/revisions/{v}/pages/{p}` | 某版某页(信封带 `version`/`page`/`total_pages`/`reason_code`,`items` 逐条带原因) |
| POST | `/admin/publish` | 发布下一修订(缺省自动生成确定性增量,可传 `reason_code` 与 `ops`) |
| POST | `/admin/reset` | 重置回首版(可指定站点数与种子) |
| GET/PUT/DELETE | `/admin/faults` | 故障注入: `missing`/`timeout`/`conflict`/`reason_mismatch`,`times` 控制触发次数 |
| GET | `/admin/state` | 源站内部状态 |

## 本地开发(不用 Docker)

```bash
pip install -r source/requirements.txt -r collector/requirements.txt
(cd source && uvicorn app.main:app --port 8001) &
(cd collector && SOURCE_BASE_URL=http://localhost:8001 DB_PATH=/tmp/collector.db \
    uvicorn app.main:app --port 8000) &
python3 scripts/smoke.py
python3 scripts/legacy_check.py   # 进程内 TestClient,无需起服务
python3 scripts/restart_check.py  # 自行在 8100/8101 端口拉起/重启真实进程
```
