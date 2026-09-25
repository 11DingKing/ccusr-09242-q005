"""洽谈时间线游标分页的自动化测试。

覆盖：
- 同秒记录：按 (held_at, id) 稳定排序，任何页大小组合都不重不漏；
- 迟到补录：按业务发生时间归位，is_late_recorded 标记与 late_only 筛选；
- 空页：无记录、翻到末尾之后；
- 边界游标：首页无游标、末页无 next_cursor、游标损坏 400、筛选变化 409；
- 重启后继续查询：无状态游标在数据库引擎重建后仍然有效；
- 旧接口兼容：不带分页参数仍返回完整列表；
- 时间线条目携带项目状态日志审计链接且链接可解析。
"""

import os
import tempfile
import unittest
from datetime import datetime

# 必须在导入 app 之前把数据库指到临时文件，避免污染开发库
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB_PATH}"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, get_db
from app import models
from app.enums import (
    Region,
    ParkType,
    ProjectStatus,
    IntentStatus,
)


API = "/api/v1"


class TimelineTestHarness:
    """每个用例独立的临时数据库，可随时 dispose 重建以模拟服务重启。"""

    def __init__(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.engine = None
        self.SessionLocal = None
        self.client = None
        self.reopen()

    def reopen(self):
        if self.engine is not None:
            self.engine.dispose()
        self.engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def close(self):
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        if os.path.exists(self.path):
            os.remove(self.path)


class NegotiationTimelineTests(unittest.TestCase):
    def setUp(self):
        self.h = TimelineTestHarness()
        self.project_id, self.intent_id, self.intent2_id = self._seed_project()

    def tearDown(self):
        self.h.close()

    # ---------- 造数辅助 ----------

    def _seed_project(self):
        db = self.h.SessionLocal()
        park = models.IndustrialPark(
            name="测试园区",
            park_type=ParkType.KEY_INDUSTRIAL,
            city="南宁市",
        )
        entity = models.Entity(
            name="测试招商主体",
            region=Region.GUANGXI,
            country_or_province="广西",
            contact_person="张三",
            contact_phone="123",
        )
        db.add_all([park, entity])
        db.flush()
        project = models.Project(
            name="测试水果深加工项目",
            status=ProjectStatus.NEGOTIATING,
            investment_direction="果汁加工",
            planned_investment_10k=1000.0,
            park_id=park.id,
            initiator_id=entity.id,
        )
        db.add(project)
        db.flush()
        intent = models.CooperationIntent(
            project_id=project.id,
            submitter_id=entity.id,
            status=IntentStatus.IN_DISCUSSION,
            cooperation_content="合作洽谈",
        )
        intent2 = models.CooperationIntent(
            project_id=project.id,
            submitter_id=entity.id,
            status=IntentStatus.IN_DISCUSSION,
            cooperation_content="第二份意向",
        )
        db.add_all([intent, intent2])
        db.commit()
        pid, iid, iid2 = project.id, intent.id, intent2.id
        db.close()
        return pid, iid, iid2

    def add_negotiation(
        self,
        intent_id,
        round_no,
        held_at,
        recorded_at=None,
        title=None,
    ):
        db = self.h.SessionLocal()
        rec = models.NegotiationRecord(
            intent_id=intent_id,
            round=round_no,
            title=title or f"第{round_no}轮洽谈",
            held_at=held_at,
            recorded_at=recorded_at if recorded_at is not None else held_at,
            created_at=recorded_at if recorded_at is not None else held_at,
            key_topics="测试议题",
        )
        db.add(rec)
        db.commit()
        rid = rec.id
        db.close()
        return rid

    def add_status_log(self, changed_at, to_status, from_status=None, log_id=None):
        db = self.h.SessionLocal()
        log = models.ProjectStatusLog(
            project_id=self.project_id,
            from_status=from_status,
            to_status=to_status,
            changed_at=changed_at,
            operator="测试员",
            reason=f"状态转为{to_status.value}",
        )
        db.add(log)
        db.commit()
        lid = log.id
        db.close()
        return lid

    def timeline_url(self, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        url = f"{API}/workflow/projects/{self.project_id}/negotiation-timeline"
        return f"{url}?{qs}" if qs else url

    def walk_pages(self, limit, **params):
        """从头到尾翻完，返回 (按顺序访问到的记录id列表, 每页返回体)。"""
        seen = []
        pages = []
        cursor = None
        while True:
            p = dict(params)
            p["limit"] = limit
            if cursor is not None:
                p["cursor"] = cursor
            resp = self.h.client.get(self.timeline_url(**p))
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            pages.append(body)
            seen.extend(item["id"] for item in body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                break
        return seen, pages

    def expected_ordered_ids(self):
        db = self.h.SessionLocal()
        rows = (
            db.query(models.NegotiationRecord.id)
            .join(
                models.CooperationIntent,
                models.NegotiationRecord.intent_id == models.CooperationIntent.id,
            )
            .filter(models.CooperationIntent.project_id == self.project_id)
            .order_by(
                models.NegotiationRecord.held_at.asc(),
                models.NegotiationRecord.id.asc(),
            )
            .all()
        )
        db.close()
        return [r[0] for r in rows]

    # ---------- 用例 ----------

    def test_empty_timeline_returns_empty_page(self):
        resp = self.h.client.get(self.timeline_url(limit=20))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["items"], [])
        self.assertIsNone(body["next_cursor"])
        self.assertFalse(body["has_more"])
        self.assertEqual(body["limit"], 20)

    def test_same_second_records_have_stable_id_tiebreak(self):
        t = datetime(2026, 2, 1, 10, 0, 0)
        ids = [
            self.add_negotiation(self.intent_id, i, t, title=f"同秒记录{i}")
            for i in range(1, 6)
            # id 自增，插入顺序即决胜顺序
        ]
        resp = self.h.client.get(self.timeline_url(limit=10))
        self.assertEqual(resp.status_code, 200)
        returned_ids = [item["id"] for item in resp.json()["items"]]
        self.assertEqual(returned_ids, ids)  # 同秒时严格按 id 升序

    def test_any_page_size_partitions_without_dup_or_gap(self):
        base = datetime(2026, 3, 1, 9, 0, 0)
        from datetime import timedelta

        created = []
        for i in range(7):
            # 故意制造多个同秒记录与不同秒记录混合
            held = base + timedelta(seconds=(i // 3) * 5)
            created.append(self.add_negotiation(self.intent_id, i, held))
        expected = self.expected_ordered_ids()
        self.assertEqual(len(expected), 7)

        for limit in range(1, 9):  # 含页大小大于总数的情况
            seen, pages = self.walk_pages(limit)
            self.assertEqual(
                seen,
                expected,
                f"limit={limit} 时顺序/完整性异常",
            )
            self.assertEqual(len(seen), len(set(seen)), f"limit={limit} 出现重复")
            # 除最后一页外每页都应取满
            for page in pages[:-1]:
                self.assertEqual(len(page["items"]), limit)
            self.assertTrue(len(pages[-1]["items"]) <= limit)

    def test_late_backfilled_record_uses_business_time_position(self):
        # 1 月 10 日正常录入的洽谈
        normal = self.add_negotiation(
            self.intent_id,
            2,
            datetime(2026, 1, 10, 10, 0, 0),
            recorded_at=datetime(2026, 1, 10, 12, 0, 0),
            title="正常录入",
        )
        # 1 月 5 日的洽谈，1 月 12 日才补录（迟到补录），入库更晚但业务时间更早
        late = self.add_negotiation(
            self.intent_id,
            1,
            datetime(2026, 1, 5, 10, 0, 0),
            recorded_at=datetime(2026, 1, 12, 9, 0, 0),
            title="迟到补录",
        )

        resp = self.h.client.get(self.timeline_url(limit=50))
        items = resp.json()["items"]
        self.assertEqual([item["id"] for item in items], [late, normal])
        by_id = {item["id"]: item for item in items}
        self.assertTrue(by_id[late]["is_late_recorded"])
        self.assertFalse(by_id[normal]["is_late_recorded"])
        # 录入时间字段被如实带出
        self.assertEqual(by_id[late]["recorded_at"][:10], "2026-01-12")

        # late_only 只返回迟到补录
        resp = self.h.client.get(self.timeline_url(limit=50, late_only="true"))
        late_items = resp.json()["items"]
        self.assertEqual([item["id"] for item in late_items], [late])

    def test_late_only_filter_can_page(self):
        normal = self.add_negotiation(
            self.intent_id, 2,
            datetime(2026, 2, 10, 10, 0, 0),
            recorded_at=datetime(2026, 2, 10, 11, 0, 0),
        )
        late1 = self.add_negotiation(
            self.intent_id, 1,
            datetime(2026, 2, 1, 10, 0, 0),
            recorded_at=datetime(2026, 2, 20, 10, 0, 0),
        )
        late2 = self.add_negotiation(
            self.intent_id, 3,
            datetime(2026, 2, 5, 10, 0, 0),
            recorded_at=datetime(2026, 2, 21, 10, 0, 0),
        )
        seen, _ = self.walk_pages(1, late_only="true")
        self.assertEqual(seen, [late1, late2])
        self.assertNotIn(normal, seen)

    def test_last_page_has_no_cursor(self):
        for i in range(3):
            self.add_negotiation(
                self.intent_id, i + 1, datetime(2026, 4, 1 + i, 10, 0, 0)
            )
        seen, pages = self.walk_pages(2)
        self.assertEqual(len(pages), 2)
        self.assertTrue(pages[0]["has_more"])
        self.assertIsNotNone(pages[0]["next_cursor"])
        self.assertFalse(pages[1]["has_more"])
        self.assertIsNone(pages[1]["next_cursor"])
        self.assertEqual(len(seen), 3)

        # 第一页游标在更大窗口下继续请求，只剩末页 1 条
        resp = self.h.client.get(
            self.timeline_url(limit=50, cursor=pages[0]["next_cursor"])
        )
        tail = resp.json()
        self.assertEqual(len(tail["items"]), 1)
        self.assertIsNone(tail["next_cursor"])

    def test_cursor_positioned_after_last_row_returns_empty_page(self):
        for i in range(2):
            self.add_negotiation(
                self.intent_id, i + 1, datetime(2026, 4, 10 + i, 10, 0, 0)
            )
        # 白盒构造一个指向“最后一条之后”的合法游标：
        # 排序键与最后一条相同、id 更大，指纹与无筛选首页一致。
        from app.services import timeline as timeline_service

        fingerprint = timeline_service.build_filter_fingerprint({
            "project_id": self.project_id,
            "intent_id": None,
            "round": None,
            "late_only": False,
            "order": "held_at:asc,id:asc",
        })
        last_id = self.expected_ordered_ids()[-1]
        cursor = timeline_service.encode_cursor(
            datetime(2026, 4, 11, 10, 0, 0), last_id, fingerprint
        )
        resp = self.h.client.get(self.timeline_url(limit=10, cursor=cursor))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["items"], [])
        self.assertIsNone(body["next_cursor"])
        self.assertFalse(body["has_more"])

    def test_garbage_cursor_is_rejected_with_400(self):
        resp = self.h.client.get(self.timeline_url(limit=10, cursor="%%%garbage%%%"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("游标", resp.json()["detail"])

    def test_cursor_becomes_invalid_when_filters_change(self):
        for i in range(4):
            self.add_negotiation(
                self.intent_id, i + 1, datetime(2026, 5, 1 + i, 9, 0, 0)
            )
            self.add_negotiation(
                self.intent2_id, i + 1, datetime(2026, 5, 1 + i, 9, 0, 0)
            )

        # 游标在 intent_id 筛选下生成
        resp = self.h.client.get(
            self.timeline_url(limit=2, intent_id=self.intent_id)
        )
        cursor = resp.json()["next_cursor"]
        self.assertIsNotNone(cursor)

        # 换了意向再带旧游标 → 409
        resp = self.h.client.get(
            self.timeline_url(limit=2, intent_id=self.intent2_id, cursor=cursor)
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("筛选条件", resp.json()["detail"])

        # 去掉筛选条件再带旧游标 → 同样 409
        resp = self.h.client.get(self.timeline_url(limit=2, cursor=cursor))
        self.assertEqual(resp.status_code, 409)

        # late_only 变化也使游标失效
        resp = self.h.client.get(
            self.timeline_url(limit=2, intent_id=self.intent_id, late_only="true", cursor=cursor)
        )
        self.assertEqual(resp.status_code, 409)

        # 原筛选条件下游标仍可正常使用
        resp = self.h.client.get(
            self.timeline_url(limit=2, intent_id=self.intent_id, cursor=cursor)
        )
        self.assertEqual(resp.status_code, 200)

    def test_continue_pagination_after_restart(self):
        for i in range(5):
            self.add_negotiation(
                self.intent_id, i + 1, datetime(2026, 6, 1 + i, 8, 0, 0)
            )
        expected = self.expected_ordered_ids()

        # 重启前：取第一页
        resp = self.h.client.get(self.timeline_url(limit=2))
        first_page = resp.json()
        self.assertEqual([i["id"] for i in first_page["items"]], expected[:2])
        cursor = first_page["next_cursor"]
        self.assertIsNotNone(cursor)

        # 模拟服务重启：销毁引擎与会话工厂，按同一数据库文件重建
        self.h.reopen()

        # 重启后用旧游标继续，顺序与完整性不受影响
        seen = [i["id"] for i in first_page["items"]]
        while cursor:
            resp = self.h.client.get(self.timeline_url(limit=2, cursor=cursor))
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            seen.extend(i["id"] for i in body["items"])
            cursor = body["next_cursor"]
        self.assertEqual(seen, expected)

    def test_legacy_negotiations_endpoint_still_returns_full_list(self):
        # 旧调用：不带任何分页参数
        for i in range(3):
            self.add_negotiation(
                self.intent_id, i + 1, datetime(2026, 7, 1 + i, 8, 0, 0)
            )
        resp = self.h.client.get(f"{API}/workflow/intents/{self.intent_id}/negotiations")
        self.assertEqual(resp.status_code, 200)
        records = resp.json()
        self.assertEqual(len(records), 3)  # 仍是一次性返回全部
        self.assertIn("recorded_at", records[0])
        # 旧排序（轮次、时间）保持不变
        self.assertEqual([r["round"] for r in records], [1, 2, 3])

    def test_audit_link_points_to_resolvable_status_log(self):
        log_negotiating = self.add_status_log(
            datetime(2026, 1, 1, 0, 0, 0),
            ProjectStatus.NEGOTIATING,
            from_status=ProjectStatus.ATTRACTING_INVESTMENT,
        )
        log_established = self.add_status_log(
            datetime(2026, 1, 20, 0, 0, 0),
            ProjectStatus.ESTABLISHED,
            from_status=ProjectStatus.NEGOTIATING,
        )
        # 洽谈发生在立项之前 → 锚定到“洽谈中”日志
        before = self.add_negotiation(
            self.intent_id, 1, datetime(2026, 1, 10, 10, 0, 0)
        )
        # 洽谈发生在立项之后 → 锚定到“已立项”日志
        after = self.add_negotiation(
            self.intent_id, 2, datetime(2026, 1, 25, 10, 0, 0)
        )

        resp = self.h.client.get(self.timeline_url(limit=10))
        items = {item["id"]: item for item in resp.json()["items"]}

        link_before = items[before]["audit_status_log"]
        link_after = items[after]["audit_status_log"]
        self.assertIsNotNone(link_before)
        self.assertIsNotNone(link_after)
        self.assertEqual(link_before["status_log_id"], log_negotiating)
        self.assertEqual(link_after["status_log_id"], log_established)
        self.assertTrue(link_before["link"].endswith(
            f"/projects/{self.project_id}/status-logs/{log_negotiating}"
        ))

        # 链接可解析
        detail = self.h.client.get(link_before["link"])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["id"], log_negotiating)

        # 新增一个项目：洽谈早于任何状态日志时不强行造链接
        h2 = TimelineTestHarness()
        try:
            db = h2.SessionLocal()
            park = models.IndustrialPark(name="无日志园区", park_type=ParkType.KEY_INDUSTRIAL, city="北海市")
            entity = models.Entity(
                name="无日志主体", region=Region.GUANGXI,
                country_or_province="广西", contact_person="李", contact_phone="9",
            )
            db.add_all([park, entity])
            db.flush()
            project = models.Project(
                name="无日志项目", status=ProjectStatus.ATTRACTING_INVESTMENT,
                investment_direction="果干加工", planned_investment_10k=10.0,
                park_id=park.id, initiator_id=entity.id,
            )
            db.add(project)
            db.flush()
            intent = models.CooperationIntent(
                project_id=project.id, submitter_id=entity.id,
                status=IntentStatus.IN_DISCUSSION, cooperation_content="x",
            )
            db.add(intent)
            db.flush()
            db.add(models.NegotiationRecord(
                intent_id=intent.id, round=1, title="早期洽谈",
                held_at=datetime(2026, 1, 1, 0, 0, 0),
                recorded_at=datetime(2026, 1, 1, 0, 0, 0),
                created_at=datetime(2026, 1, 1, 0, 0, 0),
                key_topics="t",
            ))
            db.commit()
            pid = project.id
            db.close()

            resp = h2.client.get(f"{API}/workflow/projects/{pid}/negotiation-timeline")
            item = resp.json()["items"][0]
            self.assertIsNone(item["audit_status_log"])
        finally:
            h2.close()

    def test_posted_historical_record_is_marked_late(self):
        # 通过正式接口补录一条 10 天前的洽谈，录入时间由服务端设置 → 迟到补录
        from datetime import timedelta

        held = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
        payload = {
            "round": 1,
            "title": "事后补录的洽谈",
            "held_at": held,
            "key_topics": "历史事项",
        }
        resp = self.h.client.post(
            f"{API}/workflow/intents/{self.intent_id}/negotiations", json=payload
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNotNone(resp.json()["recorded_at"])

        resp = self.h.client.get(self.timeline_url(limit=10))
        item = resp.json()["items"][0]
        self.assertEqual(item["title"], "事后补录的洽谈")
        self.assertTrue(item["is_late_recorded"])

    def test_intent_and_round_filters(self):
        i1_r1 = self.add_negotiation(
            self.intent_id, 1, datetime(2026, 8, 1, 8, 0, 0)
        )
        i1_r2 = self.add_negotiation(
            self.intent_id, 2, datetime(2026, 8, 2, 8, 0, 0)
        )
        i2_r1 = self.add_negotiation(
            self.intent2_id, 1, datetime(2026, 8, 3, 8, 0, 0)
        )

        # 按意向筛选
        resp = self.h.client.get(self.timeline_url(intent_id=self.intent_id))
        self.assertEqual(
            [i["id"] for i in resp.json()["items"]], [i1_r1, i1_r2]
        )
        resp = self.h.client.get(self.timeline_url(intent_id=self.intent2_id))
        self.assertEqual([i["id"] for i in resp.json()["items"]], [i2_r1])

        # 按轮次筛选（跨意向）
        resp = self.h.client.get(self.timeline_url(**{"round": 1}))
        self.assertEqual(
            [i["id"] for i in resp.json()["items"]], [i1_r1, i2_r1]
        )

        # 意向 + 轮次叠加
        resp = self.h.client.get(
            self.timeline_url(intent_id=self.intent_id, **{"round": 2})
        )
        self.assertEqual([i["id"] for i in resp.json()["items"]], [i1_r2])

        # 无匹配的空结果
        resp = self.h.client.get(self.timeline_url(**{"round": 9}))
        self.assertEqual(resp.json()["items"], [])

    def test_unknown_project_and_intent_return_404(self):
        resp = self.h.client.get(f"{API}/workflow/projects/99999/negotiation-timeline")
        self.assertEqual(resp.status_code, 404)
        resp = self.h.client.get(
            self.timeline_url(intent_id=99999)
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
