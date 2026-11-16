# 运行手册 qualiscore

## 1 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18204/api/health
open http://127.0.0.1:18204/
```

首次打开库里没有任何运行记录，页面只有一个空态与采信表单；选样例点
「采信并评分」即产生第一条记录。数据落在命名卷 `qualiscore-data` 的
`/data/qualiscore.sqlite`。

## 2 重建数据库

```bash
docker compose down -v          # 删掉命名卷
docker compose up -d --build    # 新库，页面回到空态
```

只想清空运行记录、保留上传的数据集时，删 SQLite 文件即可（下一次启动会建表）。

## 3 跑主流程（命令行）

```bash
# 采信脏样例
curl -s -X POST http://127.0.0.1:18204/api/datasets/import \
  -H 'Content-Type: application/json' -d '{"sample":"tickets_dirty.csv"}'

# 评分（默认样例）
curl -s -X POST http://127.0.0.1:18204/api/score \
  -H 'Content-Type: application/json' -d '{"sample":"tickets_dirty.csv"}'

# 上传自己的文件
curl -s -X POST http://127.0.0.1:18204/api/datasets/upload \
  -F 'file=@sample_data/tickets_dirty.csv' -F 'encoding=utf-8'

# 导出
curl -s -o /tmp/report.csv http://127.0.0.1:18204/api/export/report.csv
curl -s http://127.0.0.1:18204/api/export/manifest.json
```

## 4 样例

| 文件 | 用途 | 期望结果 |
|---|---|---|
| `tickets_dirty.csv` | 主演示：哨兵、空白、重复、冲突、越界、分段、截断 | 65.24 分 C 级，隔离 3 行，2 段 |
| `tickets_clean.csv` | 干净对照 | 100.00 分 A 级 |
| `tickets_gbk.csv` | GB18030 + 中文表头 | 采信成功，标 `K_ALIAS_MAPPED` |
| `tickets_bom.csv` | BOM + CRLF | 采信成功，标 `K_BOM_STRIPPED` |
| `tickets_truncated.csv` | 末行截断 | 隔离 1 行，标 `K_TRUNCATED_TAIL` |
| `tickets_constant.csv` | 常量列 + 全空列 | 70.00 分 C 级，缺失维封顶 |
| `tickets_header_only.csv` | 仅表头 | 422 `E_HEADER_ONLY` |
| `tickets_blank.csv` | 全空行 | 422 `E_ALL_BLANK` |
| `tickets_empty.csv` | 空文件 | 422 `E_EMPTY_FILE` |
| `returns_roster_48.csv` | 回传样例的计划名单 | 48 个工单 |
| `returns_attachment_partial.csv` | 回传样例的附件 | 40 单回传、2 条名单外、2 条重复、6 条缺字段 |

## 5 回传核对

计划名单 + 回传附件走一次核对。名单一行一个工单号（也接受带 `ticket_id` 表头的 CSV），
附件是 10 列契约 CSV。

```bash
# 用内置样例（名单 48 单、附件 40 单回传 + 2 条名单外 + 2 条重复 + 6 条缺字段）
curl -s -X POST http://127.0.0.1:18204/api/returns/check \
  -H 'Content-Type: application/json' -d '{"batch":"demo","sample":true}'

# 上传自己的名单与附件
curl -s -X POST http://127.0.0.1:18204/api/returns/upload \
  -F 'batch=demo' -F 'roster=@plan.csv' -F 'attachment=@returned.csv'

# 只换附件，名单沿用当前生效版本
curl -s -X POST http://127.0.0.1:18204/api/returns/upload \
  -F 'batch=demo' -F 'attachment=@returned_v2.csv'

# 版本与尝试账；四类差异明细
curl -s 'http://127.0.0.1:18204/api/returns/status?batch=demo'
curl -s -o /tmp/diffs.csv 'http://127.0.0.1:18204/api/export/returns-diffs.csv?batch=demo'
```

页面 `/returns` 同时给出三个数：回传覆盖率（0%–100%）、已回传字段质量分（0–100）、
批次分（= 质量分 × 覆盖率）。四类差异分列「未回传 / 名单外 / 重复记录 / 缺字段」，
计数单位不同不可相加。字段质量分只代表已回传记录，不能外推到未回传工单。

替换失败时接口返回 500 `E_RETURN_REPLACE_FAILED`，旧版快照与指针原样保留；
同输入重试只累计尝试账 `hits`，不新增版本。

## 6 常见失败

| HTTP | 原因码 | 处理 |
|---|---|---|
| 422 | `E_EMPTY_FILE` | 文件是 0 字节，换一份 |
| 422 | `E_HEADER_ONLY` | 只有表头，导出补上数据行 |
| 422 | `E_ALL_BLANK` | 数据行全空，检查导出是否只写了分隔符 |
| 422 | `E_HEADER_INVALID` | 缺必填列；`detail.missing` 列出缺哪几列 |
| 422 | `E_DECODE` | 编码不在 UTF-8 / GB18030 内，先转码再传 |
| 404 | `E_SAMPLE_UNKNOWN` | 样例名不在白名单，用 `/api/datasets` 看可用项 |
| 409 | `E_STALE_RUN` | 页面版本落后；刷新后重跑，别用旧页面覆盖 |
| 404 | `E_RUN_NOT_FOUND` | 还没有运行记录，先跑一次评分 |
| 422 | `E_ROSTER_EMPTY` | 计划名单为空或没有可识别的工单号 |
| 422 | `E_RETURN_NO_BASE` | 批次还没有基线版本，名单与附件要一起传 |
| 500 | `E_RETURN_REPLACE_FAILED` | 附件替换失败；旧版仍生效，重试即可，不会重复计数 |

## 7 测试

```bash
docker compose exec -T app python -m pytest -q
```

本地（不启容器）也可以：

```bash
python3 -m pytest -q
```

## 8 日志与排查

- 容器日志：`docker compose logs -f app`。
- 分数异常先看 `/batches` 的阈值表与五项对账；对账不平说明有单元格被重复计数。
- 离群数为 0 时先看 `/outliers` 的「退化列」：满意度一类 1-5 分刻度默认判退化。
- 回传批次分偏低时先看 `/returns` 的覆盖扣分与质量扣分：质量扣分高是附件字段问题，
  覆盖扣分高是名单没交齐，两者不要混着改。
- 每份导出都能在 `/api/export/manifest.json` 里找到同一次运行的 `run_id` 与输入摘要；
  回传批次看 `/api/export/returns-manifest.json`。
