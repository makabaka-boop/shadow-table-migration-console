import { FormEvent, useEffect, useMemo, useState } from 'react';
import {
  AppState,
  CommitResult,
  LegacyRow,
  MappingConfig,
  MappingRule,
  Operation,
  PreviewResult,
  RecordRow,
  api,
  defaultMapping,
  operationLabels,
} from './api';
import './styles.css';

const fields = ['id', 'code', 'name', 'age'] as const;
const sourceColumns = ['legacy_id', 'legacy_code', 'legacy_name', 'legacy_age'];
const faults = [
  { value: '', label: '不注入故障' },
  { value: 'preview-copy-abort', label: '复制中断' },
  { value: 'commit-switch-after-rename', label: '切换中途失败' },
];

const emptyLegacy: LegacyRow = {
  id: null,
  legacy_code: '',
  legacy_name: '',
  legacy_age: '',
};

function cloneMapping(mapping: MappingConfig): MappingConfig {
  return JSON.parse(JSON.stringify(mapping));
}

function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [mapping, setMapping] = useState<MappingConfig>(cloneMapping(defaultMapping));
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [commit, setCommit] = useState<CommitResult | null>(null);
  const [historyRows, setHistoryRows] = useState<RecordRow[] | null>(null);
  const [selectedHistory, setSelectedHistory] = useState<number | null>(null);
  const [draft, setDraft] = useState<LegacyRow>(emptyLegacy);
  const [fault, setFault] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    const next = await api.state();
    setState(next);
  };

  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
  }, []);

  const updateRule = (field: string, patch: Partial<MappingRule>) => {
    setMapping((current) => ({
      ...current,
      [field]: { ...current[field], ...patch },
    }));
    setPreview(null);
    setCommit(null);
  };

  const runPreview = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setNotice('');
    setCommit(null);
    setHistoryRows(null);
    setBusy(true);
    try {
      const result = await api.preview(mapping, fault);
      setPreview(result);
      setNotice(
        result.ok
          ? `预演通过：${result.total_rows} 行，源表修订号 ${result.source_revision}`
          : `预演发现 ${result.failed_rows.length} 个失败行；正式表未改变`,
      );
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const runCommit = async () => {
    if (!preview) return;
    setError('');
    setNotice('');
    setCommit(null);
    setBusy(true);
    try {
      const result = await api.commit(preview.job_id, preview.source_revision, fault);
      setCommit(result);
      setPreview(null);
      setNotice(`切换完成：正式表修订号 ${result.target_revision}，旧版已保留为 ${result.history_table}`);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveLegacy = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      const result = await api.saveLegacy(draft);
      setNotice(`旧表已保存，新的源表修订号为 ${result.legacy_revision}`);
      setDraft(emptyLegacy);
      setPreview(null);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const openHistory = async (version: number) => {
    setSelectedHistory(version);
    const result = await api.history(version);
    setHistoryRows(result.records);
  };

  const previewRevisionMismatch = useMemo(() => {
    if (!state || !preview) return false;
    return state.meta.legacy_revision !== preview.source_revision;
  }, [state, preview]);

  if (!state) {
    return <main className="page"><p>正在加载数据库状态…</p></main>;
  }

  return (
    <main className="page">
      <header>
        <div>
          <h1>SQLite 旧记录表字段映射迁移</h1>
          <p>影子表预演、逐行校验、按源表修订号原子切换，并保留只读旧版。</p>
        </div>
        <section className="revision-card" aria-label="数据库修订号">
          <span>源表修订</span>
          <strong>{state.meta.legacy_revision}</strong>
          <span>正式表修订</span>
          <strong>{state.meta.target_revision}</strong>
        </section>
      </header>

      {error && <div className="alert error" role="alert">{error}</div>}
      {notice && <div className="alert success" role="status">{notice}</div>}

      <div className="grid">
        <section className="panel">
          <h2>1. 配置有限字段映射</h2>
          <form onSubmit={runPreview} className="mapping-form">
            {fields.map((field) => {
              const rule = mapping[field] ?? { op: 'copy' as Operation, source: '' };
              return (
                <div className="mapping-row" key={field}>
                  <label>目标字段 <code>{field}</code></label>
                  <select
                    aria-label={`${field} 操作`}
                    value={rule.op}
                    onChange={(event) => updateRule(field, { op: event.target.value as Operation })}
                  >
                    {Object.entries(operationLabels).map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                  {rule.op === 'constant' ? (
                    <input
                      aria-label={`${field} 常量`}
                      value={String(rule.value ?? '')}
                      onChange={(event) => updateRule(field, { value: event.target.value })}
                    />
                  ) : (
                    <select
                      aria-label={`${field} 源列`}
                      value={rule.source ?? ''}
                      onChange={(event) => updateRule(field, { source: event.target.value })}
                    >
                      <option value="">选择源列</option>
                      {sourceColumns.map((column) => <option key={column}>{column}</option>)}
                    </select>
                  )}
                </div>
              );
            })}
            <div className="mapping-row">
              <label htmlFor="fault">故障注入</label>
              <select id="fault" value={fault} onChange={(event) => setFault(event.target.value)}>
                {faults.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </select>
            </div>
            <button disabled={busy} type="submit">复制到影子表并预演</button>
          </form>
        </section>

        <section className="panel">
          <h2>2. 模拟另一个客户端写入</h2>
          <form onSubmit={saveLegacy} className="legacy-form">
            <input placeholder="ID（新增时留空）" value={String(draft.id ?? '')}
              onChange={(e) => setDraft({ ...draft, id: e.target.value ? Number(e.target.value) : null })} />
            <input placeholder="legacy_code" value={draft.legacy_code}
              onChange={(e) => setDraft({ ...draft, legacy_code: e.target.value })} />
            <input placeholder="legacy_name" value={draft.legacy_name}
              onChange={(e) => setDraft({ ...draft, legacy_name: e.target.value })} />
            <input placeholder="legacy_age" value={draft.legacy_age}
              onChange={(e) => setDraft({ ...draft, legacy_age: e.target.value })} />
            <button disabled={busy} type="submit">写入旧表（增加修订号）</button>
          </form>
        </section>
      </div>

      {preview && (
        <section className="panel">
          <div className="section-title">
            <h2>3. 预演报告</h2>
            <div>
              <span className="badge">预演源修订 {preview.source_revision}</span>
              <span className={preview.ok ? 'badge good' : 'badge bad'}>
                {preview.ok ? '全部通过' : `${preview.failed_rows.length} 行失败`}
              </span>
            </div>
          </div>
          {previewRevisionMismatch && (
            <div className="alert warning">
              当前源表修订 {state.meta.legacy_revision} 已不同于预演修订 {preview.source_revision}；
              提交会被拒绝，必须重新预演。
            </div>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>行</th><th>旧行 ID</th><th>字段</th><th>映射来源</th><th>结果</th><th>错误</th></tr>
              </thead>
              <tbody>
                {preview.candidate_rows.map((row) => (
                  <tr key={row.row_num} className={row.status === 'invalid' ? 'invalid-row' : ''}>
                    <td>{row.row_num}</td>
                    <td>{row.legacy_id}</td>
                    <td>
                      {fields.map((field) => (
                        <div key={field}><code>{field}</code>: {String(row.values[field] ?? '∅')}</div>
                      ))}
                    </td>
                    <td>
                      {fields.map((field) => (
                        <div key={field} data-testid={`source-${row.row_num}-${field}`}>{row.sources[field]}</div>
                      ))}
                    </td>
                    <td data-testid={`status-${row.row_num}`}>{row.status === 'valid' ? '通过' : '失败'}</td>
                    <td>
                      {row.errors.map((err, index) => (
                        <div key={`${err.field}-${index}`} className="field-error">{err.message}</div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button
            className="primary"
            disabled={busy || !preview.ok || previewRevisionMismatch}
            onClick={runCommit}
          >
            提交并携带源表修订号 {preview.source_revision}
          </button>
        </section>
      )}

      {commit && (
        <section className="panel">
          <h2>提交结果</h2>
          <p>任务 <code>{commit.job_id}</code> 已在同一事务中完成表切换。</p>
        </section>
      )}

      <div className="grid">
        <section className="panel">
          <h2>当前正式表（修订 {state.meta.target_revision}）</h2>
          <div className="table-wrap">
            <table>
              <thead><tr><th>ID</th><th>Code</th><th>Name</th><th>Age</th><th>来源修订</th></tr></thead>
              <tbody>
                {state.records.map((row) => (
                  <tr key={row.id}><td>{row.id}</td><td>{row.code}</td><td>{row.name}</td><td>{row.age}</td><td>{row.source_revision}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel">
          <h2>旧记录与只读历史</h2>
          <h3>旧表</h3>
          <div className="table-wrap compact">
            <table>
              <thead><tr><th>ID</th><th>Code</th><th>Name</th><th>Age text</th></tr></thead>
              <tbody>
                {state.legacy_records.map((row) => (
                  <tr key={row.id}><td>{row.id}</td><td>{row.legacy_code}</td><td>{row.legacy_name}</td><td>{row.legacy_age}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>保留版本</h3>
          {state.history.length === 0 ? <p>尚无切换历史。</p> : (
            <>
              <div className="history-buttons">
                {state.history.map((item) => (
                  <button key={item.version} onClick={() => openHistory(item.version)}>查看 v{item.version}</button>
                ))}
              </div>
              {selectedHistory !== null && historyRows && (
                <div className="table-wrap compact">
                  <p>v{selectedHistory} 为只读历史，共 {historyRows.length} 行。</p>
                  <table>
                    <thead><tr><th>ID</th><th>Code</th><th>Name</th><th>Age</th><th>来源修订</th></tr></thead>
                    <tbody>
                      {historyRows.map((r) => (
                        <tr key={r.id}><td>{r.id}</td><td>{r.code}</td><td>{r.name}</td><td>{r.age}</td><td>{r.source_revision}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}

export default App;
