# 🔎 OpenAPI 版本兼容性审查工具

对比新旧两份 **OpenAPI 3.x**（JSON / YAML）文档，递归解析 `$ref`（**支持循环引用**）、合并 `allOf`，按**请求 / 响应两个方向**的语义规则判断每一处变更是否向后兼容，精确定位破坏性变更的 JSON 路径并给出原因。支持结果筛选、引用展开树浏览、审查报告下载、**单条发现的最小复现包导出**与历史记录。

技术栈：**FastAPI + SQLite + 原生 JavaScript**（前端零构建、零框架依赖）。

---

## 🚀 一条命令启动

需要安装 Docker 与 Docker Compose v2：

```bash
docker compose up -d --build
```

启动后访问：

> **http://localhost:8000**

端口说明：容器内监听 `8000`，通过 `docker-compose.yml` 映射到宿主机 **8000**；审查数据保存在 Docker 卷 `review-data`（容器内 `/data/reviews.db`），重启不丢失。

停止 / 更新：

```bash
docker compose down          # 停止（保留数据卷）
docker compose up -d --build # 修改代码后重新构建
```

如需换端口，修改 `docker-compose.yml` 中 `"8000:8000"` 的左侧宿主机端口即可。

### 本地直接运行（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 🧭 使用方式

1. 打开 http://localhost:8000 ，在左右两栏**粘贴**或点击「上传」选择旧版 / 新版 OpenAPI 文件（`.json` / `.yaml` / `.yml` 均可，也可混用）。
2. 可先点击内置示例：**典型破坏性变更 / 循环 $ref / 纯兼容新增 / 枚举约束收窄**。
3. 点击「开始审查 →」。
4. 结果区提供：
   - 顶部统计卡片与总体结论（✅ 向后兼容 / ❌ N 项破坏性变更）；
   - **级别筛选**：🔴 破坏性 / 🟢 兼容 / 🟡 废弃提示 / ⚪ 信息；
   - **方向筛选**（请求 / 响应 / 结构性）、**分类筛选**、关键字搜索；
   - 每条发现可展开：人类可读定位、旧/新版 JSON Pointer、`$ref` 锚点、旧值 / 新值、规则编号；
   - 点击发现项上的 **🌲旧 / 🌲新** 按钮，右侧「引用展开树」自动切到对应版本并展开到该 `$ref`；
   - 每条发现可点击 **📦 导出最小复现包**，下载仅含该发现现场的最小文档对（ZIP，详见下文）；
   - 「⬇ 下载 Markdown / JSON 报告」获取可归档的审查报告。
5. 「历史记录」标签页可重新打开、下载或删除保存在 SQLite 中的历史审查。

---

## 🧠 判定原理

同一类 schema 变化在两个方向上结论可能相反，工具按方向独立判定：

| 视角 | 核心原则 |
| --- | --- |
| **请求方向**（参数、requestBody） | 客户端原本合法发送的内容**被收紧**即为破坏：必填变多、枚举变少、范围收窄、类型收窄等 |
| **响应方向**（responses） | 客户端原本基于契约的预期**被改变**即为破坏：字段消失、枚举变多、承诺范围扩大、变 nullable、字段变 writeOnly 等 |

部分规则示例：

| 变更 | 请求方向 | 响应方向 |
| --- | :---: | :---: |
| 新增必填字段 | 🔴 破坏 | 🟢 兼容（保证更多数据） |
| 新增可选字段 | 🟢 兼容 | 🟢 兼容（严格模型需确认） |
| 删除字段 | 🟢 兼容* | 🔴 破坏 |
| 可选 → 必填 | 🔴 破坏 | 🟢 兼容 |
| 必填 → 可选 | 🟢 兼容 | 🔴 破坏（字段可能消失） |
| 枚举取值增加 | 🟢 兼容 | 🔴 破坏 |
| 枚举取值减少 | 🔴 破坏 | 🟢 兼容 |
| `minimum` 升高 / `maximum` 降低 | 🔴 破坏（收紧） | 🟢 兼容 |
| `minimum` 降低 / `maximum` 升高 | 🟢 兼容 | 🔴 破坏（承诺漂移） |
| 变 `nullable` | 🟢 兼容 | 🔴 破坏 |
| 字段变 `readOnly` | 🔴 破坏 | 🟢 兼容 |
| 字段变 `writeOnly` | 🟢 兼容 | 🔴 破坏 |
| `additionalProperties: true→false` | 🔴 破坏 | 🟢 兼容 |

\* 请求字段从契约移除时服务端可能开始忽略/拒绝，工具以兼容记录并在原因中注明。

结构层面还覆盖：路径与操作的新增/删除、pathItem 级与操作级参数合并比较、参数必填性 / `style` / `explode` / `deprecated`、请求体必填性变化、媒体类型增删（请求 415 / 响应 Content-Type）、响应状态码增删、`servers` 增删、`operationId` 变化、`oneOf/anyOf` 分支增删等。

> 页面内「规则说明」标签有完整列表；每条发现都带稳定的 `rule_id`（如 `property-became-required`、`enum-value-added`），便于在 CI 中按规则处理。

### `$ref` 与循环引用处理

- 参数、请求体、响应、schema 中的 `$ref` 在比较前先解引用；`allOf` 按合取语义合并（属性深度合并、`required` 取并集、min 取大 / max 取小、枚举取交集）。
- schema 递归比较同时维护 **DFS 活跃栈**与**已比较节点对集合**：遇到 A→B→A 形式的自引用 / 互相引用时立即终止该分支，且同一条差异不会沿环路重复刷屏；循环节点在展开树中以 **⟳ 循环引用** 高亮。
- 兼容 OpenAPI 3.0（`nullable: true`）与 3.1（`type: ["string","null"]`）两种可空写法。

发现项给出**双重定位**：

- `location`：载荷语义路径，如 `requestBody > application/json → 请求体.address.zip`
- `old_json_path` / `new_json_path`：文档内 JSON Pointer（引用组件时锚点指向 `/components/schemas/...`），可直接用于在引用树中跳转。

---

## 📦 报告格式

- **Markdown**：结论、统计概览、按严重度排列的明细表（级别/分类/方向/端点/定位/原因），以及可折叠的 JSON Pointer 与旧值/新值附录。
- **JSON**：结构化结果，包含 `summary` 与全部 `findings`（含 `rule_id`、`severity`、`direction`、`location`、`old_json_path`、`new_json_path`、`old_value`、`new_value` 等），适合接入 CI/CD 质量门禁。

---

## 📦 最小复现包（Minimal Repro）

针对**单条发现**导出一个可独立复现该差异的最小文档包，便于提交 issue、回归测试或在其他工具中复现。

**使用方式**：展开任意发现项 → 点击「📦 导出最小复现包」→ 下载 ZIP。新审查与「历史记录」中重新打开的旧审查均可导出。

**裁剪规则**：

- 旧、新两份文档各自只保留：文档头（`openapi` / `info`，服务器类发现含 `servers`）、该发现所属的 **path / operation**（操作级发现同时保留 path 级 `parameters`，因为引擎会将其合并进操作比较）；
- 从保留内容出发做**本地 `$ref` 闭包**：传递引用到的 `components`（schemas / parameters / requestBodies / pathItems 等）按原指针位置复制进最小文档，闭包用 visited 集合**去重**、对 A→B→A 这类**循环引用安全终止**；
- 未被引用的路径与组件一律剔除。

**导出前复核**：裁剪完成后，后端会用同一差异引擎对最小文档对重新比对，确认目标发现的 `rule_id`、方向、端点与语义定位仍然存在；**复核失败时返回可读错误（HTTP 409），不提供失真的包**。

**ZIP 内容**：

| 文件 | 说明 |
| --- | --- |
| `old.yaml` | 裁剪后的旧版最小文档（可直接重新上传审查） |
| `new.yaml` | 裁剪后的新版最小文档 |
| `manifest.json` | 原审查 ID / 标题 / 时间、发现指纹（语义身份字段的 SHA-256 短哈希）、发现详情（rule_id、方向、端点、定位、JSON Pointer 等）与复核结论 |

`manifest.json` 示例：

```json
{
  "format": "openapi-repro/1",
  "generated_at": "2026-09-18T08:00:00+00:00",
  "review": { "id": 7, "title": "Petstore 1.4.0 → 2.0.0", "..." : "..." },
  "finding": {
    "id": 12,
    "fingerprint": "3f8c1a2b9d0e4f56",
    "rule_id": "property-became-required",
    "direction": "request",
    "endpoint": "POST /pets",
    "location": "requestBody > application/json → 请求体.status"
  },
  "verification": { "status": "passed", "engine": "run_diff" },
  "files": ["old.yaml", "new.yaml", "manifest.json"]
}
```

---

## 🗂️ 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI：审查 / 历史 / 引用树 / 报告下载接口
│   ├── parser.py      # JSON·YAML 载入与 OpenAPI 3.x 校验
│   ├── refs.py        # JSON Pointer、$ref 解析、循环安全的引用展开树
│   ├── diff.py        # 语义兼容性规则引擎（请求/响应双方向）
│   ├── repro.py       # 最小复现包：文档裁剪、$ref 闭包、复核与 ZIP 打包
│   ├── reports.py     # Markdown / JSON 报告生成
│   └── database.py    # SQLite 持久化（评审、原文、结果）
├── static/
│   ├── index.html     # 单页界面
│   ├── styles.css
│   ├── app.js         # 原生 JS：筛选 / 展开树 / 历史 / 下载
│   └── samples.js     # 四组内置示例（JSON + YAML）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 🔌 HTTP API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/reviews` | 提交 `{old_spec, new_spec, ...}` 执行审查并持久化 |
| `GET` | `/api/reviews` | 历史审查列表 |
| `GET` | `/api/reviews/{id}` | 审查详情（含全部 findings） |
| `DELETE` | `/api/reviews/{id}` | 删除审查 |
| `GET` | `/api/reviews/{id}/pointers` | 引用树根节点分组（paths / components） |
| `GET` | `/api/reviews/{id}/tree?side=old|new&pointer=/...` | 引用展开树（含循环标记） |
| `GET` | `/api/reviews/{id}/report?format=markdown|json` | 下载报告 |
| `GET` | `/api/reviews/{id}/findings/{fid}/repro` | 导出单条发现的最小复现包（ZIP；复核失败返回 409 及可读原因） |

交互式文档：http://localhost:8000/docs （FastAPI 自动生成）。

## ⚠️ 范围说明

- 仅支持 OpenAPI **3.0 / 3.1**；Swagger 2.0 会被明确拒绝并提示。
- 仅解析**文档内** `$ref`；外部文件引用（`./other.yaml#/...`）会在树中标记为无法解析。
- 规则基于 OpenAPI 社区通行的破坏性变更分类（参考 OASdiff 的方向化判定），属于静态契约分析，不验证具体业务逻辑。
