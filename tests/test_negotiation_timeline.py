"""洽谈时间线游标分页与迟到补录语义的自动化测试。

覆盖：同秒记录稳定排序、迟到补录、空页、边界游标（篡改/筛选变化/末页）、
重启后继续查询、跨页无重复无遗漏、旧调用兼容，以及状态日志审计链接。
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app import models
from app.enums import (
    Region,
    ParkType,
    ProjectStatus,
    IntentStatus,
)


def _iso(dt):
    return dt.isoformat()


class TimelineTestBase(unittest.TestCase):
    def setUp(self):
        # 每个用例使用独立的临时 SQLite 文件，便于模拟“重启后继续查询”。
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self._override_get_db()

        self.client = TestClient(app)
        self._seed_base()

    def _override_get_db(self, engine=None):
        engine = engine or self.engine
        session_local = sessionmaker(bind=engine)

        def _get_db():
            db = session_local()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _get_db

    def tearDown(self):
        app.dependency_overrides.clear()
        self.engine.dispose()
        os.remove(self.db_path)

    def _seed_base(self):
        """建立 园区/主体/项目/意向 的最小关联骨架。"""
        db = self.Session()
        self.park = models.IndustrialPark(
            name="测试园区", park_type=ParkType.BORDER_PORT, city="崇左市"
        )
        self.entity = models.Entity(
            name="测试主体",
            region=Region.GUANGXI,
            country_or_province="广西",
            contact_person="张三",
            contact_phone="123",
        )
        db.add_all([self.park, self.entity])
        db.flush()

        self.project = models.Project(
            name="长期谈判项目",
            project_code="TEST-001",
            status=ProjectStatus.NEGOTIATING,
            investment_direction="水果深加工",
            planned_investment_10k=1000.0,
            park_id=self.park.id,
            initiator_id=self.entity.id,
        )
        db.add(self.project)
        db.flush()

        self.intent = models.CooperationIntent(
            project_id=self.project.id,
            submitter_id=self.entity.id,
            status=IntentStatus.IN_DISCUSSION,
            cooperation_content="合资洽谈",
        )
        db.add(self.intent)
        db.flush()
        self.project_id = self.project.id
        self.intent_id = self.intent.id
        self.park_id = self.park.id
        self.entity_id = self.entity.id
        db.commit()
        db.close()

    def add_negotiation(
        self,
        title,
        held_at,
        recorded_at=None,
        round_no=1,
        intent_id=None,
    ):
        db = self.Session()
        target_intent_id = intent_id or self.intent_id
        rec = models.NegotiationRecord(
            intent_id=target_intent_id,
            round=round_no,
            title=title,
            held_at=held_at,
            recorded_at=recorded_at or held_at,
            key_topics="条款",
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        db.close()
        return rec.id

    def add_status_log(self, changed_at, to_status=ProjectStatus.NEGOTIATING,
                       from_status=None, reason="状态变更"):
        db = self.Session()
        log = models.ProjectStatusLog(
            project_id=self.project_id,
            from_status=from_status,
            to_status=to_status,
            changed_at=changed_at,
            reason=reason,
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        db.close()
        return log.id

    def add_second_intent_with_project(self, code="TEST-002", project_name="另一项目"):
        db = self.Session()
        proj = models.Project(
            name=project_name,
            project_code=code,
            status=ProjectStatus.NEGOTIATING,
            investment_direction="冷链",
            planned_investment_10k=2000.0,
            park_id=self.park_id,
            initiator_id=self.entity_id,
        )
        db.add(proj)
        db.flush()
        intent = models.CooperationIntent(
            project_id=proj.id,
            submitter_id=self.entity_id,
            status=IntentStatus.IN_DISCUSSION,
            cooperation_content="另一洽谈",
        )
        db.add(intent)
        db.commit()
        pid, iid = proj.id, intent.id
        db.close()
        return pid, iid

    # -- 工具：用游标翻完整个时间线，返回按访问顺序的 id 列表 ----------------
    def walk(self, query="", page_size=2):
        ids = []
        cursor = None
        seen_pages = 0
        while True:
            params = f"limit={page_size}"
            if cursor:
                params += f"&cursor={cursor}"
            if query:
                params += f"&{query}"
            resp = self.client.get(f"/api/v1/workflow/negotiations/timeline?{params}")
            self.assertEqual(resp.status_code, 200, resp.text)
            page = resp.json()
            self.assertIsInstance(page, dict)
            ids.extend(item["id"] for item in page["items"])
            seen_pages += 1
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                break
            cursor = page["next_cursor"]
            self.assertIsNotNone(cursor)
            self.assertLess(seen_pages, 1000)
        return ids


class SameSecondTests(TimelineTestBase):
    def test_same_second_records_use_id_as_tie_breaker(self):
        t = datetime(2026, 3, 1, 10, 0, 0)
        ids = [
            self.add_negotiation(f"同秒记录{i}", t, recorded_at=t, round_no=i)
            for i in range(1, 4)
        ]
        resp = self.client.get("/api/v1/workflow/negotiations/timeline?limit=10")
        items = resp.json()["items"]
        # held_at 完全相同（同秒），以稳定标识 id 降序决胜，顺序确定。
        self.assertEqual([it["id"] for it in items], sorted(ids, reverse=True))
        self.assertTrue(all(it["held_at"] == _iso(t) for it in items))


class NoDuplicateNoGapTests(TimelineTestBase):
    def test_any_page_size_covers_every_record_once(self):
        base = datetime(2026, 1, 1, 9, 0, 0)
        all_ids = []
        for i in range(7):
            held = base + timedelta(days=i, hours=(i % 3))
            all_ids.append(
                self.add_negotiation(f"第{i}轮", held, recorded_at=held, round_no=i)
            )
        # 多种页大小都应恰好覆盖全部记录、无重复、无遗漏。
        for size in (1, 2, 3, 5, 7, 100):
            with self.subTest(page_size=size):
                walked = self.walk(page_size=size)
                self.assertEqual(sorted(walked), sorted(all_ids))
                self.assertEqual(len(walked), len(set(walked)))


class LateBackfillTests(TimelineTestBase):
    def test_late_backfill_positioned_by_business_time_and_flagged(self):
        now = datetime(2026, 6, 1, 12, 0, 0)
        # 正常按时记录
        id_recent = self.add_negotiation(
            "近期洽谈", now - timedelta(days=1),
            recorded_at=now - timedelta(days=1), round_no=2,
        )
        # 迟到补录：业务发生在 30 天前，但今天才录入
        id_late = self.add_negotiation(
            "迟到补录的历史洽谈", now - timedelta(days=30),
            recorded_at=now, round_no=1,
        )
        # 当天补写（间隔 1 小时）不视为迟到
        id_sameday = self.add_negotiation(
            "当天整理纪要", now - timedelta(hours=2),
            recorded_at=now - timedelta(hours=1), round_no=3,
        )
        resp = self.client.get("/api/v1/workflow/negotiations/timeline?limit=10")
        items = resp.json()["items"]
        by_id = {it["id"]: it for it in items}
        self.assertTrue(by_id[id_late]["is_late"])
        self.assertFalse(by_id[id_recent]["is_late"])
        self.assertFalse(by_id[id_sameday]["is_late"])
        # 排序按业务发生时间降序：当天 → 近期 → 迟到补录（补录不因其录入晚而插队）。
        self.assertEqual(
            [it["id"] for it in items],
            [id_sameday, id_recent, id_late],
        )


class EmptyPageTests(TimelineTestBase):
    def test_empty_timeline_first_page(self):
        resp = self.client.get("/api/v1/workflow/negotiations/timeline?limit=10")
        page = resp.json()
        self.assertEqual(page["items"], [])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])


class BoundaryCursorTests(TimelineTestBase):
    def test_cursor_after_final_item_returns_empty_page(self):
        from app.pagination import encode_cursor

        t1 = datetime(2026, 2, 1, 9, 0, 0)
        t2 = datetime(2026, 2, 2, 9, 0, 0)
        id_old = self.add_negotiation("较早记录", t1, recorded_at=t1)
        id_new = self.add_negotiation("较晚记录", t2, recorded_at=t2)
        first = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=1"
        ).json()
        self.assertEqual([it["id"] for it in first["items"]], [id_new])
        self.assertTrue(first["has_more"])
        second = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=1"
            f"&cursor={first['next_cursor']}"
        ).json()
        self.assertEqual([it["id"] for it in second["items"]], [id_old])
        self.assertFalse(second["has_more"])

        # 边界游标：定位在最后一条记录自身之上（等价于“末页之后”），稳定返回空页。
        from app.pagination import build_filter_fingerprint
        fp = build_filter_fingerprint({"project_id": None, "intent_id": None})
        boundary = encode_cursor(t1.isoformat(), id_old, fp)
        tail = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=1&cursor={boundary}"
        ).json()
        self.assertEqual(tail["items"], [])
        self.assertFalse(tail["has_more"])
        self.assertIsNone(tail["next_cursor"])
        # 同一游标重复请求结果一致，不报错。
        tail_again = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=1&cursor={boundary}"
        ).json()
        self.assertEqual(tail_again["items"], [])

    def test_truncated_cursor_is_rejected(self):
        t = datetime(2026, 2, 1, 9, 0, 0)
        self.add_negotiation("记录一", t, recorded_at=t)
        self.add_negotiation("记录二", t + timedelta(days=1), recorded_at=t)
        cursor = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=1"
        ).json()["next_cursor"]
        # 截断游标：base64 可解码但 JSON 已损坏。
        resp = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=1"
            f"&cursor={cursor[: len(cursor) // 2]}"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], "invalid_cursor")

    def test_garbage_cursor_is_rejected(self):
        resp = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=1&cursor=not-a-cursor!!!"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], "invalid_cursor")

    def test_cursor_invalidated_when_filter_changes(self):
        t = datetime(2026, 2, 1, 9, 0, 0)
        # 本项目两条，保证 limit=1 时第一页就签发游标
        self.add_negotiation("项目记录A", t, recorded_at=t)
        self.add_negotiation("项目记录B", t - timedelta(days=2), recorded_at=t)
        pid2, iid2 = self.add_second_intent_with_project()
        self.add_negotiation(
            "另一项目记录", t, recorded_at=t, intent_id=iid2
        )
        # 游标在 project_id 筛选下签发
        cursor = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=1&project_id={self.project_id}"
        ).json()["next_cursor"]
        # 换筛选条件复用旧游标 -> 明确的 stale_cursor 错误
        resp = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=1"  # 不带 project_id
            f"&cursor={cursor}"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], "stale_cursor")

        # 换成另一个 project_id 同样失效
        resp2 = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=1&project_id={pid2}"
            f"&cursor={cursor}"
        )
        self.assertEqual(resp2.status_code, 400)
        self.assertEqual(resp2.json()["detail"]["code"], "stale_cursor")

        # 原筛选条件下游标仍然可用
        ok = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=1"
            f"&project_id={self.project_id}&cursor={cursor}"
        )
        self.assertEqual(ok.status_code, 200)


class FilterTests(TimelineTestBase):
    def test_project_and_intent_filters(self):
        t = datetime(2026, 2, 1, 9, 0, 0)
        id1 = self.add_negotiation("本项目", t, recorded_at=t)
        pid2, iid2 = self.add_second_intent_with_project()
        id2 = self.add_negotiation(
            "另一项目", t + timedelta(days=1), recorded_at=t, intent_id=iid2,
        )
        r1 = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=10&project_id={self.project_id}"
        ).json()
        self.assertEqual([it["id"] for it in r1["items"]], [id1])
        r2 = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=10&intent_id={iid2}"
        ).json()
        self.assertEqual([it["id"] for it in r2["items"]], [id2])


class RestartResumeTests(TimelineTestBase):
    def test_resume_after_restart_with_persistent_cursor(self):
        base = datetime(2026, 4, 1, 8, 0, 0)
        ids = [
            self.add_negotiation(
                f"r{i}", base + timedelta(days=i),
                recorded_at=base + timedelta(days=i), round_no=i,
            )
            for i in range(5)
        ]
        # 第一页：limit=2
        first = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=2"
        ).json()
        first_ids = [it["id"] for it in first["items"]]
        self.assertEqual(len(first_ids), 2)
        cursor = first["next_cursor"]

        # 模拟服务重启：丢弃引擎/连接池与所有会话，用新引擎指向同一个数据库文件。
        app.dependency_overrides.clear()
        self.engine.dispose()
        restarted_engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        self._override_get_db(engine=restarted_engine)
        self.engine = restarted_engine

        # 用重启前签发的游标继续，剩余记录不重复、不遗漏。
        rest = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=2&cursor={cursor}"
        ).json()
        rest_ids = [it["id"] for it in rest["items"]]
        cursor2 = rest["next_cursor"]
        final = self.client.get(
            f"/api/v1/workflow/negotiations/timeline?limit=2&cursor={cursor2}"
        ).json()
        rest_ids += [it["id"] for it in final["items"]]

        self.assertEqual(sorted(first_ids + rest_ids), sorted(ids))
        self.assertEqual(len(set(first_ids + rest_ids)), len(ids))

    def test_new_late_insert_during_pagination_never_duplicates(self):
        base = datetime(2026, 5, 1, 8, 0, 0)
        ids = [
            self.add_negotiation(
                f"r{i}", base + timedelta(days=i),
                recorded_at=base + timedelta(days=i), round_no=i,
            )
            for i in range(4)
        ]
        first = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=2"
        ).json()
        cursor = first["next_cursor"]

        # 翻页过程中迟到补录一条业务时间更早的历史记录。
        late_id = self.add_negotiation(
            "翻页期间补录的更早记录",
            base - timedelta(days=10),
            recorded_at=base + timedelta(days=20),
        )
        # 继续翻页不会重复第一页的记录（补录落在更早的页面，游标之后不再回访）。
        walked = [it["id"] for it in first["items"]]
        cur = cursor
        while True:
            page = self.client.get(
                f"/api/v1/workflow/negotiations/timeline?limit=2&cursor={cur}"
            ).json()
            walked += [it["id"] for it in page["items"]]
            if not page["has_more"]:
                break
            cur = page["next_cursor"]
        self.assertEqual(len(walked), len(set(walked)))
        for fid in [it["id"] for it in first["items"]]:
            self.assertIn(fid, walked)
        # 从第一页重新查询（补录后新遍历）必然能看到迟到记录，无遗漏。
        fresh = self.walk(page_size=2)
        self.assertIn(late_id, fresh)
        self.assertEqual(sorted(fresh), sorted(ids + [late_id]))


class CompatibilityTests(TimelineTestBase):
    def test_legacy_call_without_paging_returns_bare_list(self):
        t = datetime(2026, 1, 1, 9, 0, 0)
        self.add_negotiation("记录一", t, recorded_at=t)
        self.add_negotiation("记录二", t + timedelta(days=1), recorded_at=t)
        # 旧调用：不带 limit/cursor，返回裸列表（而非分页对象）。
        resp = self.client.get("/api/v1/workflow/negotiations/timeline")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 2)
        # 显式分页则返回对象结构。
        paged = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=1"
        ).json()
        self.assertIsInstance(paged, dict)
        self.assertIn("next_cursor", paged)
        self.assertIn("has_more", paged)


class AuditLinkTests(TimelineTestBase):
    def test_timeline_items_link_to_status_logs(self):
        held = datetime(2026, 3, 10, 14, 0, 0)
        self.add_negotiation("需要审计的洽谈", held, recorded_at=held)
        log_id = self.add_status_log(
            changed_at=held - timedelta(days=1),
            reason="进入洽谈中",
        )
        page = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=10"
        ).json()
        item = page["items"][0]
        logs_url = item["project_status_logs_url"]
        self.assertEqual(
            logs_url, f"/api/v1/projects/{self.project_id}/status-logs"
        )
        # 页面级审计链接
        self.assertEqual(
            page["audit_links"][str(self.project_id)], logs_url
        )
        # 审计链接可访问
        r_logs = self.client.get(logs_url)
        self.assertEqual(r_logs.status_code, 200)
        self.assertEqual(len(r_logs.json()), 1)

        # 最近状态锚点链接可访问
        nearest_url = item["nearest_status_log_url"]
        self.assertEqual(
            nearest_url, f"/api/v1/projects/{self.project_id}/status-logs/{log_id}"
        )
        r_one = self.client.get(nearest_url)
        self.assertEqual(r_one.status_code, 200)
        self.assertEqual(r_one.json()["id"], log_id)

    def test_nearest_status_log_anchors_to_business_time(self):
        held = datetime(2026, 3, 10, 14, 0, 0)
        self.add_negotiation("洽谈", held, recorded_at=datetime.utcnow())
        before_id = self.add_status_log(
            changed_at=held - timedelta(days=2), reason="洽谈前的状态"
        )
        after_id = self.add_status_log(
            changed_at=held + timedelta(days=2),
            to_status=ProjectStatus.ESTABLISHED,
            reason="洽谈后立项",
        )
        item = self.client.get(
            "/api/v1/workflow/negotiations/timeline?limit=10"
        ).json()["items"][0]
        # 锚点按业务发生时间 held_at 定位，而不是录入时间。
        self.assertEqual(item["nearest_status_log"]["id"], before_id)
        self.assertNotEqual(item["nearest_status_log"]["id"], after_id)


if __name__ == "__main__":
    unittest.main()
