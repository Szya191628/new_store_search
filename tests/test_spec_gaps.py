import io
import os
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DB_TYPE"] = "sqlite"
os.environ["SQLITE_PATH"] = _tmp_db.name
os.environ["ENABLE_PLAYWRIGHT"] = "false"

from fastapi.testclient import TestClient
from app.main import app
from app.db import init_db


class TestSpecClosure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)

    def _login(self, username: str, pin: str) -> str:
        res = self.client.post("/api/auth/login", json={"username": username, "pin": pin})
        self.assertEqual(res.status_code, 200)
        return res.json()["token"]

    def test_batch_assign_and_release_flow(self):
        admin_token = self._login("admin", "123456")
        sales1_token = self._login("sales1", "1234")

        csv_bytes = (
            "企业名称,统一社会信用代码,注册地址,联系电话,所属行业\n"
            "成都市青羊区老王五金店,92510105MA100001,成都市青羊区宽窄巷子10号底商,13800001111,零售业\n"
            "成都市青羊区小赵面馆,92510105MA100002,成都市青羊区宽窄巷子12号一层,13800002222,餐饮业\n"
        ).encode("utf-8-sig")

        imp = self.client.post(
            "/api/leads/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            files={"file": ("batch_assign.csv", csv_bytes, "text/csv")},
        )
        self.assertEqual(imp.status_code, 200)

        leads = self.client.get(
            "/api/leads?pool=public",
            headers={"Authorization": f"Bearer {admin_token}"},
        ).json()["leads"]
        ids = [l["id"] for l in leads if l["credit_code"] in ("92510105MA100001", "92510105MA100002")]
        self.assertEqual(len(ids), 2)

        users = self.client.get("/api/users", headers={"Authorization": f"Bearer {admin_token}"}).json()["users"]
        sales2 = next(u for u in users if u["username"] == "sales2")

        # 非管理员批量指派应被拒绝 (403)
        forbidden = self.client.post(
            "/api/leads/batch-assign",
            headers={"Authorization": f"Bearer {sales1_token}"},
            json={"lead_ids": ids, "user_id": sales2["id"]},
        )
        self.assertEqual(forbidden.status_code, 403)

        # 管理员批量指派给 sales2
        assigned = self.client.post(
            "/api/leads/batch-assign",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"lead_ids": ids, "user_id": sales2["id"]},
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.json()["assigned_count"], 2)

        # sales1 无权把 sales2 的线索退回公海 (403)，管理员可以退回
        rel_forbidden = self.client.post(
            f"/api/leads/{ids[0]}/release",
            headers={"Authorization": f"Bearer {sales1_token}"},
        )
        self.assertEqual(rel_forbidden.status_code, 403)

        rel_ok = self.client.post(
            f"/api/leads/{ids[0]}/release",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(rel_ok.status_code, 200)

    def test_xlsx_multirow_header_and_amap_verification_funnel(self):
        admin_token = self._login("admin", "123456")

        # 构造带顶部说明行的 Excel (.xlsx)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["企查查导出报表 - 2026-11-18"])
        ws.append(["个体户名称", "信用代码", "经营场所", "有效手机号", "所属行业", "经营范围"])
        ws.append(["成都市锦江区阿花茶饮店", "92510104MA200001", "成都市锦江区春熙路88号一层铺面", "13511112222", "餐饮业", "现制茶饮"])
        ws.append(["成都市锦江区老刘汽修部", "92510104MA200002", "成都市锦江区红星路18号门面", "13533334444", "居民服务", "汽车维修"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        imp = self.client.post(
            "/api/leads/import",
            headers={"Authorization": f"Bearer {admin_token}"},
            files={"file": ("qcc.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        self.assertEqual(imp.status_code, 200)
        self.assertEqual(imp.json()["summary"]["imported_rows"], 2)

        # 管理员配置高德 Key，普通销售访问配置应 403
        sales1_token = self._login("sales1", "1234")
        self.assertEqual(
            self.client.get("/api/settings", headers={"Authorization": f"Bearer {sales1_token}"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.put(
                "/api/settings",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"amap_api_key": "fake-amap-key", "default_city": "成都市"},
            ).status_code,
            200,
        )

        # Mock 高德 POI 返回：阿花茶饮命中周边 POI -> 升为 A 级；老刘汽修未命中 -> 保持 S 级
        def fake_check_poi(self, address, core_trade_name, city=""):
            if "阿花" in core_trade_name:
                return {
                    "lng": 104.08,
                    "lat": 30.65,
                    "amap_poi_found": 1,
                    "amap_poi_name": "阿花茶饮(春熙路店)",
                    "amap_poi_distance": 25,
                    "amap_match_score": 90,
                }
            return {
                "lng": 104.09,
                "lat": 30.66,
                "amap_poi_found": 0,
                "amap_poi_name": "",
                "amap_poi_distance": None,
                "amap_match_score": 10,
            }

        with patch("app.services.verifier.AmapClient.check_store_poi", new=fake_check_poi):
            ver = self.client.post(
                "/api/verify/run?limit=10",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        self.assertEqual(ver.status_code, 200)
        self.assertGreaterEqual(ver.json()["result"]["amap_checked"], 2)

        # 按街道筛选并验证分级与高德导航链接
        chunxi = self.client.get(
            "/api/leads?street_name=春熙路",
            headers={"Authorization": f"Bearer {sales1_token}"},
        ).json()["leads"]
        self.assertEqual(len(chunxi), 1)
        self.assertEqual(chunxi[0]["grade"], "A")
        self.assertIn("uri.amap.com/marker", chunxi[0]["jump_urls"]["amap_nav_url"])

        stats = self.client.get("/api/stats", headers={"Authorization": f"Bearer {sales1_token}"}).json()
        self.assertGreaterEqual(stats["total"], 4)


if __name__ == "__main__":
    unittest.main()
