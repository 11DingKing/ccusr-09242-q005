# 水果深加工招商台账后端服务

记录合作主体、园区、项目、洽谈、立项、里程碑和投产后的产能兑现情况，为招商团队提供可追溯的业务接口。

## 运行约定

服务端代码位于 `app` 目录，默认使用项目目录中的 SQLite 文件保存业务数据。环境变量可以覆盖数据库位置和接口前缀，临时配置不应提交到仓库。

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q app tests
```

## 启动服务

准备依赖后可执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，根路径返回服务状态，接口文档位于 `/docs`。

## 洽谈时间线（游标分页）

招商主管按业务发生时间回看项目洽谈记录时，使用：

```
GET /api/v1/workflow/projects/{project_id}/negotiation-timeline
```

- 固定按 `held_at`（业务发生时间）升序、`id` 升序排列，`id` 是同秒记录的稳定并列决胜键，任何翻页组合都不重复、不遗漏。
- 分页参数：`limit`（1~200，默认 50）、`cursor`（首页不传，取上一页响应中的 `next_cursor`）。游标无状态，仅记录排序键与筛选指纹，服务重启后仍可继续翻页。
- 筛选参数：`intent_id`、`round`、`late_only=true`。**筛选条件变化后旧游标失效**：损坏游标返回 `400`，筛选不一致返回 `409`，需从第一页重新开始。
- 迟到补录：记录另有 `recorded_at`（录入时间，服务端写入）。录入晚于发生超过 24 小时的条目标记 `is_late_recorded=true`，但仍按业务发生时间归位。
- 每条记录的 `audit_status_log` 给出该时点最近一条项目状态变更日志的审计链接（`GET /projects/{id}/status-logs/{log_id}`）。
- 旧接口 `GET /api/v1/workflow/intents/{intent_id}/negotiations` 保持不变，不带分页参数仍一次性返回该意向的全部洽谈记录。

