# SQLite 旧记录表有限字段映射迁移

示例包含：

- React 页面配置四种有限映射：`copy`、`trim`、`parse_decimal`、`constant`。
- FastAPI 将旧表快照复制到带诊断信息的影子表，并逐行报告非空、十进制整数、整数范围、主键/唯一约束。
- 预演失败只保留诊断影子表，正式表和正式表修订号不变。
- 提交必须携带预演依据的源表修订号；若旧表期间被其他客户端写入，返回 `409` 并要求重新预演。
- 切换在单个 SQLite 写事务中完成：`records` 重命名为只读历史表、受完整约束的影子表重命名为新 `records`、更新修订号与历史元数据。
- `X-Fault-Inject` 头支持复制中断、切换中途失败；后端测试核对原子性、修订号和历史表内容。

## 启动后端

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
MIGRATION_AUTO_APP=1 MIGRATION_DB=./migration.db uvicorn app.main:app --reload --port 8000
```

接口：

- `GET /api/state`：源表/正式表修订号、当前记录、历史版本。
- `POST /api/preview`：执行影子表复制和逐行预演。
- `POST /api/migrations/{job_id}/commit`：携带 `source_revision` 原子切换。
- `POST /api/legacy`：新增或更新旧表，触发器递增源表修订号。
- `GET /api/history/{version}`：读取只读历史版本。

## 启动前端

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 `http://localhost:5173`。页面内置故障注入选择器和模拟另一客户端写旧表的表单。

## 测试

```bash
cd backend
PYTHONPATH=. pytest
```

```bash
cd frontend
npm test
npm run build
```
