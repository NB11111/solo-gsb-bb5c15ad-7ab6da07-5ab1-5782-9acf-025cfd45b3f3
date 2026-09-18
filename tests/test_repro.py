"""最小复现包（app.repro）的核心保证测试。

零额外依赖，直接运行：
    python -m unittest discover -s tests
或：
    python tests/test_repro.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.diff import run_diff  # noqa: E402
from app.parser import load_spec  # noqa: E402
from app.repro import (  # noqa: E402
    ReproError,
    build_package,
    collect_component_closure,
    finding_fingerprint,
    prune_document,
)

OLD = """
openapi: 3.0.3
info: {title: Shop, version: "1"}
servers:
  - {url: https://api.example.com/v1}
  - {url: https://legacy.example.com}
paths:
  /orders:
    get:
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Order'}
    post:
      requestBody: {required: false, content: {application/json: {schema: {$ref: '#/components/schemas/Order'}}}}
      responses: {'201': {description: c}}
  /unused:
    get:
      responses: {'200': {description: u, content: {application/json: {schema: {$ref: '#/components/schemas/Unused'}}}}}
components:
  schemas:
    Order:
      type: object
      required: [id]
      properties:
        id: {type: string}
        status: {type: string, enum: [new, paid, cancelled]}
        customer: {$ref: '#/components/schemas/Customer'}
        self: {$ref: '#/components/schemas/Order'}
    Customer:
      type: object
      properties:
        address: {$ref: '#/components/schemas/Address'}
    Address: {type: object, properties: {city: {type: string}}}
    Unused: {type: object}
"""

NEW = """
openapi: 3.0.3
info: {title: Shop, version: "2"}
servers:
  - {url: https://api.example.com/v1}
paths:
  /orders:
    get:
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Order'}
    post:
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/Order'}}}}
      responses: {'201': {description: c}}
components:
  schemas:
    Order:
      type: object
      required: [id, amount]
      properties:
        id: {type: integer}
        amount: {type: integer, minimum: 10}
        status: {type: string, enum: [new, paid]}
        customer: {$ref: '#/components/schemas/Customer'}
        self: {$ref: '#/components/schemas/Order'}
    Customer:
      type: object
      properties:
        address: {$ref: '#/components/schemas/Address'}
    Address: {type: object, properties: {city: {type: string}}}
    BrandNew: {type: object}
  securitySchemes:
    ApiKey: {type: apiKey, in: header, name: X-API-Key}
security:
  - ApiKey: []
"""


class ReproPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old = load_spec(OLD)
        cls.new = load_spec(NEW)
        cls.findings = run_diff(cls.old, cls.new)
        cls.review = {"id": 42, "title": "Shop", "created_at": "now",
                      "old_version": "1", "new_version": "2",
                      "old_filename": None, "new_filename": None}

    def _finding(self, rule_id: str, contains: str | None = None):
        for f in self.findings:
            if f["rule_id"] == rule_id and (
                contains is None or contains in f["location"]
            ):
                return f
        self.fail(f"找不到发现 {rule_id}")

    def _unzip(self, pkg: bytes) -> tuple[dict, dict, dict]:
        zf = zipfile.ZipFile(io.BytesIO(pkg))
        self.assertEqual(set(zf.namelist()), {"old.yaml", "new.yaml", "manifest.json"})
        return (
            load_spec(zf.read("old.yaml").decode()),
            load_spec(zf.read("new.yaml").decode()),
            json.loads(zf.read("manifest.json")),
        )

    def test_closure_transitive_dedup_and_cycle(self) -> None:
        closure = collect_component_closure(self.old, [self.old["paths"]["/orders"]])
        self.assertIn("/components/schemas/Order", closure)
        self.assertIn("/components/schemas/Customer", closure)
        self.assertIn("/components/schemas/Address", closure)  # 传递引用
        self.assertNotIn("/components/schemas/Unused", closure)
        # 自引用不导致无限递归；闭包是指针集合，天然去重
        self.assertIsInstance(closure, set)

    def test_prune_removes_unreferenced(self) -> None:
        f = self._finding("enum-value-removed")
        mo = prune_document(self.old, f, root_level=False)
        self.assertEqual(list(mo["paths"]), ["/orders"])
        names = set(mo["components"]["schemas"])
        self.assertEqual(names, {"Order", "Customer", "Address"})
        # 循环自引用结构保留
        self.assertEqual(
            mo["components"]["schemas"]["Order"]["properties"]["self"]["$ref"],
            "#/components/schemas/Order",
        )
        # 不修改原文档
        self.assertIn("/unused", self.old["paths"])

    def test_package_manifest_and_reverification(self) -> None:
        f = self._finding("enum-value-removed")
        pkg, manifest = build_package(self.old, self.new, f, self.review)
        mo, mn, m = self._unzip(pkg)

        self.assertEqual(m["review"]["id"], 42)
        self.assertEqual(m["finding"]["rule_id"], "enum-value-removed")
        self.assertEqual(m["finding"]["id"], f["id"])
        self.assertEqual(len(m["finding_fingerprint"]), 64)
        self.assertTrue(m["verification"]["matched"])
        self.assertEqual(manifest, m)

        # 用同一引擎复核：包内重跑仍产生同一 rule_id/方向/端点/语义定位
        again = run_diff(mo, mn)
        self.assertTrue(any(
            g["rule_id"] == f["rule_id"]
            and g["direction"] == f["direction"]
            and g["endpoint"] == f["endpoint"]
            and g["location"] == f["location"]
            for g in again
        ))
        # 新侧未引用组件被剔除
        self.assertNotIn("BrandNew", mn["components"]["schemas"])
        # securitySchemes 经 security 头闭包带入
        self.assertIn("securitySchemes", mn.get("components", {}))

    def test_fingerprint_stability(self) -> None:
        f = dict(self._finding("enum-value-removed"))
        fp = finding_fingerprint(f)
        f["message"] = "文案变化"
        f["old_value"], f["new_value"] = 1, 2
        self.assertEqual(finding_fingerprint(f), fp)

    def test_request_direction_finding(self) -> None:
        f = self._finding("request-body-became-required")
        mo, mn, m = self._unzip(build_package(self.old, self.new, f, self.review)[0])
        self.assertEqual(list(mo["paths"]), ["/orders"])
        self.assertEqual(m["finding"]["direction"], "request")
        self.assertTrue(m["verification"]["matched"])

    def test_root_level_finding_keeps_servers_only(self) -> None:
        f = self._finding("server-removed")
        mo, _mn, m = self._unzip(build_package(self.old, self.new, f, self.review)[0])
        self.assertEqual(
            [s["url"] for s in mo["servers"]],
            ["https://api.example.com/v1", "https://legacy.example.com"],
        )
        self.assertEqual(mo.get("paths"), {})
        self.assertNotIn("components", mo)
        self.assertTrue(m["verification"]["matched"])

    def test_one_sided_path_removal(self) -> None:
        import copy
        old_d, new_d = copy.deepcopy(self.old), copy.deepcopy(self.new)
        old_d["paths"]["/gone"] = {"delete": {"responses": {"204": {"description": "x"}}}}
        f = next(x for x in run_diff(old_d, new_d)
                 if x["rule_id"] == "path-removed" and x["endpoint"] == "/gone")
        mo, mn, m = self._unzip(build_package(old_d, new_d, f, self.review)[0])
        self.assertEqual(list(mo["paths"]), ["/gone"])
        self.assertEqual(mn.get("paths"), {})
        self.assertTrue(m["verification"]["matched"])

    def test_verification_failure_raises_readable_error(self) -> None:
        f = dict(self._finding("enum-value-removed"))
        f["endpoint"] = "GET /does-not-exist"
        with self.assertRaises(ReproError) as ctx:
            build_package(self.old, self.new, f, self.review)
        self.assertIn("复核失败", str(ctx.exception))

    def test_dangling_anchor_rejected(self) -> None:
        f = dict(self._finding("enum-value-removed"))
        f["new_json_path"] = "/components/schemas/Ghost/properties/x"
        f["new_ref"] = "#/components/schemas/Ghost"
        with self.assertRaises(ReproError):
            build_package(self.old, self.new, f, self.review)


if __name__ == "__main__":
    unittest.main(verbosity=2)
