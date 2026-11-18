import os
import tempfile
import unittest

# 测试模式使用临时 SQLite 数据库，完整验证 FastAPI 与全部核心清洗/分级逻辑
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DB_TYPE"] = "sqlite"
os.environ["SQLITE_PATH"] = _tmp_db.name
os.environ["ENABLE_PLAYWRIGHT"] = "false"

from fastapi.testclient import TestClient
from app.main import app
from app.db import init_db
from app.services.classifier import (
    classify_store_track,
    extract_core_trade_name,
    is_national_chain,
    is_physical_store_address,
)
from app.services.amap_client import compute_name_similarity
from app.services.verifier import compute_lead_grade


class TestNewStoreRadar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)

    def _login(self, username: str, pin: str) -> str:
        res = self.client.post("/api/auth/login", json={"username": username, "pin": pin})
        self.assertEqual(res.status_code, 200)
        return res.json()["token"]

    def test_classifier_and_name_extraction(self):
        # 临街五金店保留
        ok_hw, _ = is_physical_store_address("成都市武侯区玉林南路18号附2号一层门面")
        self.assertTrue(ok_hw)
        # 纯住宅房间剔除
        ok_res, _ = is_physical_store_address("成都市高新区XX小区3栋2单元1502室")
        self.assertFalse(ok_res)
        # 双轨标签
        self.assertEqual(classify_store_track("老张五金建材店", "五金零售", "五金交电销售"), "便民实体型")
        self.assertEqual(classify_store_track("张记隆江猪脚饭", "正餐服务", "餐饮服务"), "到店消费型")
        # 全国连锁剔除
        self.assertTrue(is_national_chain("瑞幸咖啡（成都春熙路店）"))
        self.assertFalse(is_national_chain("成都市武侯区张记隆江猪脚饭餐饮店"))
        # 字号提取与模糊匹配
        core = extract_core_trade_name("成都市武侯区张记隆江猪脚饭餐饮店")
        self.assertEqual(core, "张记隆江猪脚饭")
        self.assertGreaterEqual(compute_name_similarity(core, "张记猪脚饭(玉林店)"), 60)

    def test_grading(self):
        self.assertEqual(compute_lead_grade(0, "not_found", "not_found")["grade"], "S")
        self.assertEqual(compute_lead_grade(1, "unclaimed_poi", "not_found")["grade"], "A")
        self.assertEqual(compute_lead_grade(1, "not_found", "listed")["grade"], "B")
        self.assertEqual(compute_lead_grade(1, "claimed", "listed")["grade"], "D")

    def test_end_to_end_import_dedup_agency_and_claim(self):
        admin_token = self._login("admin", "123456")
        sales1_token = self._login("sales1", "1234")
        sales2_token = self._login("sales2", "1234")

        csv_content = (
            "企业名称,统一社会信用代码,注册地址,联系电话,所属行业,经营范围\n"
            "成都市武侯区阿强五金店,92510107MA000001,成都市武侯区玉林南路12号底商,13800138001,零售业,五金产品零售\n"
            "成都市武侯区小李面馆,92510107MA000002,成都市武侯区玉林南路18号一层,13900139000,餐饮业,面食餐饮服务\n"
            "成都市武侯区小王奶茶店,92510107MA000003,成都市武侯区玉林南路20号商铺,13900139000,餐饮业,现制茶饮\n"
            "成都市高新区网店工作室,92510107MA000004,成都市高新区阳光小区2栋1单元802室,13700137000,零售业,互联网销售\n"
            "瑞幸咖啡高新店,91510100MA000005,成都市高新区天府大道100号一层,13600136000,餐饮业,咖啡饮品\n"
        ).encode("utf-8-sig")

        # 第一次导入：5行中应入库3家（过滤住宅网店和瑞幸），且13900139000触发2家代账标红
        res = self.client.post(
            "/api/leads/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            files={"file": ("qichacha_export.csv", csv_content, "text/csv")},
        )
        self.assertEqual(res.status_code, 200)
        summary = res.json()["summary"]
        self.assertEqual(summary["imported_rows"], 3)
        self.assertEqual(summary["filtered_invalid"], 2)
        self.assertEqual(summary["agency_flagged"], 2)

        # 重复导入同一文件：应全部去重跳过
        res2 = self.client.post(
            "/api/leads/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            files={"file": ("qichacha_export.csv", csv_content, "text/csv")},
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["summary"]["skipped_dup"], 3)

        # 查询线索列表并验证跳转链接与双轨标签
        leads_res = self.client.get(
            "/api/leads",
            headers={"Authorization": f"Bearer {sales1_token}"},
        )
        leads = leads_res.json()["leads"]
        self.assertEqual(len(leads), 3)
        hw_lead = next(x for x in leads if "五金" in x["license_name"])
        self.assertEqual(hw_lead["store_track"], "便民实体型")
        self.assertIn("douyin_search_url", hw_lead["jump_urls"])

        # sales1 认领防撞单，sales2 再认领同一条应返回 409
        claim1 = self.client.post(
            f"/api/leads/{hw_lead['id']}/claim",
            headers={"Authorization": f"Bearer {sales1_token}"},
        )
        self.assertEqual(claim1.status_code, 200)

        claim2 = self.client.post(
            f"/api/leads/{hw_lead['id']}/claim",
            headers={"Authorization": f"Bearer {sales2_token}"},
        )
        self.assertEqual(claim2.status_code, 409)

        # 人工复核标为 B级 并写跟进
        mv = self.client.post(
            f"/api/leads/{hw_lead['id']}/manual-verify",
            headers={"Authorization": f"Bearer {sales1_token}"},
            json={"douyin_status": "not_found", "meituan_status": "listed"},
        )
        self.assertEqual(mv.status_code, 200)
        self.assertEqual(mv.json()["grade"], "B")

        fu = self.client.post(
            f"/api/leads/{hw_lead['id']}/followup",
            headers={"Authorization": f"Bearer {sales1_token}"},
            json={"follow_status": "已加微信", "note": "老板对豆包找店很感兴趣"},
        )
        self.assertEqual(fu.status_code, 200)


if __name__ == "__main__":
    unittest.main()
