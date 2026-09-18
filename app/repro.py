"""发现项"最小复现包"导出。

从某条发现项出发，分别裁剪旧 / 新两份 OpenAPI 文档：
  1. 仅保留该发现所属的 path（及 operation 所在的 pathItem）；
  2. 保留必要文档头（openapi / info；根级发现保留 servers；保留 security）；
  3. 从保留子树出发，收集所有文档内 ``$ref`` 传递闭包到的本地 components；
     闭包按指针去重，天然安全处理循环引用；
  4. 未被引用的路径与组件一律剔除。

裁剪完成后，**必须**用现有差异引擎（diff.run_diff）对裁剪后的两份文档
重新比对，确认目标 rule_id / 方向 / 端点 / 语义定位仍然存在、且原 JSON
Pointer 锚点仍可解析；任何一项不满足都抛出 ReproError，拒绝产出可能
失真的复现包。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any

import yaml

from .diff import run_diff
from .parser import SpecParseError, load_spec
from .refs import PointerError, encode_token, normalize_pointer, resolve_pointer

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
METHOD_SET = {m.upper() for m in HTTP_METHODS}

# 文档头中始终保留的字段；components/paths 由闭包与发现项决定
ALWAYS_KEEP_HEADERS = ("openapi", "info")
# OpenAPI 3.x components 下的全部具名容器（3.1 的 pathItems 等也覆盖）
COMPONENT_SECTIONS = (
    "schemas", "responses", "parameters", "examples", "requestBodies",
    "headers", "securitySchemes", "links", "callbacks", "pathItems",
)

_ENDPOINT_RE = re.compile(r"^([A-Z]+)\s+(\S.*)$")


class ReproError(Exception):
    """无法生成可信的最小复现包（找不到发现项 / 复核未通过）。"""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------- #
# 发现指纹
# ---------------------------------------------------------------------- #
def finding_fingerprint(finding: dict[str, Any]) -> str:
    """发现项语义指纹：对稳定身份字段做规范化 JSON 后取 SHA-256。

    不含序号 / 旧值新值 / message 文案，使同一语义发现在重跑或小幅
    文案调整后仍保持一致。
    """
    identity = {
        "rule_id": finding.get("rule_id"),
        "direction": finding.get("direction"),
        "change": finding.get("change"),
        "category": finding.get("category"),
        "endpoint": finding.get("endpoint"),
        "location": finding.get("location"),
        "old_json_path": finding.get("old_json_path"),
        "new_json_path": finding.get("new_json_path"),
        "old_ref": finding.get("old_ref"),
        "new_ref": finding.get("new_ref"),
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- #
# 保留根的选择
# ---------------------------------------------------------------------- #
def _split_endpoint(endpoint: str | None) -> tuple[str | None, str | None]:
    """把 "GET /x" 拆成 ("/x", "get")；"/x" 拆成 ("/x", None)。"""
    if not endpoint:
        return None, None
    m = _ENDPOINT_RE.match(endpoint.strip())
    if m and m.group(1) in METHOD_SET:
        return m.group(2).strip(), m.group(1).lower()
    if endpoint.startswith("/"):
        return endpoint.strip(), None
    return None, None


def select_paths(
    doc: dict[str, Any], finding: dict[str, Any]
) -> list[str]:
    """该发现在某一侧文档中需要保留的 path（原样路径字符串，按文档顺序）。

    - 端点类发现：保留端点所属路径（若该侧文档中存在）；
    - servers / openapi-version 等根级发现：无路径。
    """
    path, _method = _split_endpoint(finding.get("endpoint"))
    if not path:
        return []
    paths = doc.get("paths")
    if isinstance(paths, dict) and path in paths:
        return [path]
    return []


# ---------------------------------------------------------------------- #
# $ref 传递闭包
# ---------------------------------------------------------------------- #
def _is_component_pointer(pointer: str) -> bool:
    parts = pointer.lstrip("/").split("/")
    return len(parts) >= 3 and parts[0] == "components"


def _component_owner(pointer: str) -> tuple[str, str] | None:
    """``/components/<section>/<name>/...`` → (section, name)。"""
    parts = pointer.lstrip("/").split("/")
    if len(parts) >= 3 and parts[0] == "components":
        return parts[1], parts[2]
    return None


def collect_component_closure(
    doc: dict[str, Any],
    roots: list[Any],
    extra_component_refs: list[str] | None = None,
) -> set[str]:
    """从 roots 子树出发收集可达的本地 components 指针集合。

    - 以指针去重，重复 / 循环引用不会重复遍历，不会无限递归；
    - 外部文件 $ref 无法在本地解析，原样保留在文档中、不进入闭包；
    - 悬空（目标不存在）的内部 $ref 跳过目标遍历。
    """
    component_pointers: set[str] = set()
    enqueued_refs: set[str] = set()
    stack: list[Any] = list(roots)

    def enqueue_ref(ref: str) -> None:
        try:
            pointer = normalize_pointer(ref)
        except PointerError:
            return  # 外部引用或非法引用：无本地组件可收
        if not pointer or pointer in enqueued_refs:
            return
        enqueued_refs.add(pointer)
        if not _is_component_pointer(pointer):
            return  # 非 components 目标（如跨 path 指针）不拉入额外路径
        component_pointers.add(pointer)
        try:
            target = resolve_pointer(doc, pointer)
        except PointerError:
            return  # 悬空引用
        stack.append(target)

    for ref in extra_component_refs or []:
        enqueue_ref(ref)

    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                enqueue_ref(ref)
                # OpenAPI 3.1 允许 $ref 与兄弟字段并存，兄弟字段仍需遍历
            # discriminator.mapping 的取值是裸 $ref 字符串（不在 $ref 键下）
            disc = node.get("discriminator")
            if isinstance(disc, dict) and isinstance(disc.get("mapping"), dict):
                for mapped_ref in disc["mapping"].values():
                    if isinstance(mapped_ref, str):
                        enqueue_ref(mapped_ref)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)

    return component_pointers


def _security_scheme_refs(doc: dict[str, Any], kept_paths: list[str]) -> list[str]:
    """根级与被保留操作级 security 引用到的 securitySchemes。"""
    names: set[str] = set()

    def harvest(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    names.update(str(k) for k in item)

    harvest(doc.get("security"))
    paths = doc.get("paths")
    if isinstance(paths, dict):
        for p in kept_paths:
            item = paths.get(p)
            if isinstance(item, dict):
                harvest(item.get("security"))
                for method in HTTP_METHODS:
                    op = item.get(method)
                    if isinstance(op, dict):
                        harvest(op.get("security"))
    return [
        f"#/components/securitySchemes/{encode_token(name)}" for name in names
    ]


# ---------------------------------------------------------------------- #
# 文档裁剪
# ---------------------------------------------------------------------- #
def prune_document(
    doc: dict[str, Any],
    finding: dict[str, Any],
    *,
    root_level: bool,
) -> dict[str, Any]:
    """按发现项裁剪单侧文档，返回新的 dict（不修改原文）。"""
    kept_paths = select_paths(doc, finding)

    mini: dict[str, Any] = {}
    for key in ALWAYS_KEEP_HEADERS:
        if key in doc:
            mini[key] = doc[key]

    # 根级发现（servers / openapi 版本）：servers 是定位本体，必须保留
    if root_level and isinstance(doc.get("servers"), list):
        mini["servers"] = doc["servers"]

    # security 本身不参与差异判定，原样保留并由闭包收齐被引用的方案
    if isinstance(doc.get("security"), list):
        mini["security"] = doc["security"]

    paths = doc.get("paths")
    mini_paths: dict[str, Any] = {}
    path_roots: list[Any] = []
    if isinstance(paths, dict):
        for p in kept_paths:
            mini_paths[p] = paths[p]
            path_roots.append(paths[p])
    mini["paths"] = mini_paths

    extra_refs = _security_scheme_refs(doc, kept_paths)
    closure = collect_component_closure(doc, path_roots, extra_refs)

    # 把闭包按具名组件归组（整条具名组件都保留，含其未被指到的兄弟字段）
    wanted: dict[str, set[str]] = {}
    for pointer in closure:
        owner = _component_owner(pointer)
        if owner is None:
            continue
        section, name = owner
        wanted.setdefault(section, set()).add(name)

    components = doc.get("components")
    if isinstance(components, dict) and wanted:
        mini_components: dict[str, Any] = {}
        for section in COMPONENT_SECTIONS:
            names = wanted.get(section)
            holder = components.get(section)
            if not names or not isinstance(holder, dict):
                continue
            # 按原文档顺序保留被引用的具名组件
            mini_components[section] = {
                name: holder[name] for name in holder if name in names
            }
        if mini_components:
            mini["components"] = mini_components

    return mini


def is_root_level_finding(finding: dict[str, Any]) -> bool:
    path, _ = _split_endpoint(finding.get("endpoint"))
    return path is None


# ---------------------------------------------------------------------- #
# 裁剪后复核
# ---------------------------------------------------------------------- #
def verify_finding(
    mini_old: dict[str, Any],
    mini_new: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    """用差异引擎重跑裁剪文档，确认目标发现仍然存在。

    匹配条件：rule_id + 方向 + 变更类型 + 端点 + 语义定位，
    且原 old/new JSON Pointer 锚点在裁剪文档中仍可解析。
    失败时抛 ReproError（可读中文错误）。
    """
    re_findings = run_diff(mini_old, mini_new)

    matched: dict[str, Any] | None = None
    for f in re_findings:
        if (
            f.get("rule_id") == target.get("rule_id")
            and f.get("direction") == target.get("direction")
            and f.get("change") == target.get("change")
            and f.get("endpoint") == target.get("endpoint")
            and f.get("location") == target.get("location")
        ):
            matched = f
            break

    if matched is None:
        direction_cn = {
            "request": "请求方向",
            "response": "响应方向",
            "shared": "请求/响应",
            "none": "结构",
        }.get(target.get("direction"), target.get("direction"))
        endpoint = target.get("endpoint") or "（文档级）"
        raise ReproError(
            "复核失败：对裁剪后的两份文档重新执行差异比对，"
            f"未再产生规则 {target.get('rule_id')!r}"
            f"（{direction_cn}，端点 {endpoint}，定位 {target.get('location')!r}）的发现项。"
            "为避免提供失真的最小复现包，导出已中止。"
        )

    # 锚点仍须可解析。引擎的 json_path 在解引用后可能把语义后缀拼到
    # 锚点之后（如经自引用进入组件内部），且历史数据里的路径锚点未必是
    # 严格编码的 JSON Pointer，因此这里按锚点"根"做结构校验：
    #   - $ref 组件锚点：具名组件必须仍在包内；
    #   - /paths 锚点：由发现端点定位，对应 path（及 operation）必须仍在；
    #   - /components、/servers 等：直接按指针解析。
    endpoint_path, endpoint_method = _split_endpoint(target.get("endpoint"))
    missing: list[str] = []
    for side, mini, json_path, ref in (
        ("旧版", mini_old, target.get("old_json_path"), target.get("old_ref")),
        ("新版", mini_new, target.get("new_json_path"), target.get("new_ref")),
    ):
        problem = _anchor_missing(
            mini, json_path, ref, endpoint_path, endpoint_method
        )
        if problem:
            missing.append(f"{side}{problem}")
    if missing:
        raise ReproError(
            "复核失败：目标发现虽再次出现，但其文档定位锚点 "
            + "、".join(missing)
            + " 在裁剪后的文档中已缺失，导出已中止以避免失真。"
        )

    return {
        "matched": True,
        "matched_finding_seq": matched.get("id"),
        "total_findings_in_package": len(re_findings),
        "matched_rule_id": matched.get("rule_id"),
    }


def _anchor_missing(
    mini: dict[str, Any],
    json_path: str | None,
    ref: str | None,
    endpoint_path: str | None,
    endpoint_method: str | None,
) -> str | None:
    """锚点在裁剪文档中缺失时返回描述，存在返回 None。"""
    if not json_path:
        return None
    if ref:
        try:
            pointer = normalize_pointer(ref)
        except PointerError:
            return f"引用 {ref}"
        try:
            resolve_pointer(mini, pointer)
        except PointerError:
            return f"引用组件 {pointer}"
        # 真实引擎输出中 ref 锚点与 json_path 的组件根必然一致；
        # 不一致只可能来自陈旧 / 失真的历史数据，按锚点缺失处理。
        if json_path.startswith("/components/"):
            parts = json_path.lstrip("/").split("/")
            comp_root = "/" + "/".join(parts[:3])
            if comp_root != pointer:
                return f"组件 {comp_root}"
        return None
    if json_path.startswith("/paths/") and endpoint_path:
        paths = mini.get("paths")
        if not isinstance(paths, dict) or endpoint_path not in paths:
            return f"路径 {endpoint_path}"
        if endpoint_method:
            item = paths.get(endpoint_path)
            if not isinstance(item, dict) or not isinstance(
                item.get(endpoint_method), dict
            ):
                return f"端点 {endpoint_method.upper()} {endpoint_path}"
        return None
    if json_path.startswith("/components/"):
        parts = json_path.lstrip("/").split("/")
        root = "/" + "/".join(parts[:3])
        try:
            resolve_pointer(mini, root)
        except PointerError:
            return f"组件 {root}"
        return None
    if json_path.startswith("/servers"):
        if not isinstance(mini.get("servers"), list):
            return "servers"
        return None
    return None  # /openapi 等无固定结构的根级锚点不做字面校验


# ---------------------------------------------------------------------- #
# YAML 输出
# ---------------------------------------------------------------------- #
def dump_yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(
        doc,
        allow_unicode=True,
        sort_keys=False,
        width=100,
        default_flow_style=False,
    )


# ---------------------------------------------------------------------- #
# 打包
# ---------------------------------------------------------------------- #
def build_package(
    old_doc: dict[str, Any],
    new_doc: dict[str, Any],
    finding: dict[str, Any],
    review: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    """生成最小复现包 ZIP，返回 (zip 字节, manifest)。"""
    root_level = is_root_level_finding(finding)
    mini_old = prune_document(old_doc, finding, root_level=root_level)
    mini_new = prune_document(new_doc, finding, root_level=root_level)

    # 输出为 YAML 再解析回来：确保交付物本身是可解析的 OpenAPI 3.x
    old_yaml = dump_yaml(mini_old)
    new_yaml = dump_yaml(mini_new)
    try:
        parsed_old = load_spec(old_yaml, "old.yaml")
        parsed_new = load_spec(new_yaml, "new.yaml")
    except SpecParseError as exc:
        raise ReproError(f"复核失败：裁剪结果不是合法的 OpenAPI 3.x 文档：{exc}")

    verification = verify_finding(parsed_old, parsed_new, finding)

    fingerprint = finding_fingerprint(finding)
    manifest = {
        "kind": "openapi-review-minimal-repro",
        "manifest_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "openapi-compat-review",
        "review": {
            "id": review.get("id"),
            "title": review.get("title"),
            "created_at": review.get("created_at"),
            "old_filename": review.get("old_filename"),
            "new_filename": review.get("new_filename"),
            "old_version": review.get("old_version"),
            "new_version": review.get("new_version"),
        },
        "finding": {
            "id": finding.get("id"),
            "rule_id": finding.get("rule_id"),
            "severity": finding.get("severity"),
            "category": finding.get("category"),
            "direction": finding.get("direction"),
            "change": finding.get("change"),
            "endpoint": finding.get("endpoint"),
            "location": finding.get("location"),
            "old_json_path": finding.get("old_json_path"),
            "new_json_path": finding.get("new_json_path"),
            "old_ref": finding.get("old_ref"),
            "new_ref": finding.get("new_ref"),
            "message": finding.get("message"),
        },
        "finding_fingerprint": fingerprint,
        "fingerprint_algorithm": (
            "sha256(canonical-json({rule_id, direction, change, category, "
            "endpoint, location, old_json_path, new_json_path, old_ref, new_ref}))"
        ),
        "files": {
            "old": "old.yaml",
            "new": "new.yaml",
        },
        "package_note": (
            "仅包含该发现所属 path/operation、必要文档头及其传递引用到的本地 "
            "components；导出前已用同一差异引擎复核目标发现仍然存在。"
        ),
        "verification": verification,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("old.yaml", old_yaml)
        zf.writestr("new.yaml", new_yaml)
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return buffer.getvalue(), manifest
