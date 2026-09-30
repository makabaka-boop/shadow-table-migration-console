export type Operation = 'copy' | 'trim' | 'parse_decimal' | 'constant';

export interface MappingRule {
  op: Operation;
  source?: string | null;
  value?: string | number | null;
}

export type MappingConfig = Record<string, MappingRule>;

export interface Meta {
  legacy_revision: number;
  target_revision: number;
  active_table: string;
}

export interface LegacyRow {
  id: number | null;
  legacy_code: string;
  legacy_name: string;
  legacy_age: string;
}

export interface RecordRow {
  id: number;
  code: string;
  name: string;
  age: number;
  source_revision: number;
}

export interface HistorySummary {
  version: number;
  source_revision: number;
  table_name: string;
  committed_at: string;
}

export interface AppState {
  meta: Meta;
  legacy_records: LegacyRow[];
  records: RecordRow[];
  history: HistorySummary[];
}

export interface FieldError {
  field: string;
  code: string;
  message: string;
  source: string;
}

export interface PreviewRow {
  row_num: number;
  legacy_id: number;
  values: Record<string, unknown>;
  sources: Record<string, string>;
  status: 'valid' | 'invalid';
  errors: FieldError[];
}

export interface PreviewResult {
  job_id: string;
  ok: boolean;
  source_revision: number;
  target_revision: number;
  total_rows: number;
  failed_rows: PreviewRow[];
  candidate_rows: PreviewRow[];
}

export interface CommitResult {
  ok: boolean;
  job_id: string;
  source_revision: number;
  target_revision: number;
  history_table: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail ?? `请求失败：${response.status}`);
  }
  return body as T;
}

export const api = {
  state: () => request<AppState>('/api/state'),
  preview: (mapping: MappingConfig, fault = '') =>
    request<PreviewResult>('/api/preview', {
      method: 'POST',
      body: JSON.stringify({ mapping }),
      headers: {
        'Content-Type': 'application/json',
        ...(fault ? { 'X-Fault-Inject': fault } : {}),
      },
    }),
  commit: (jobId: string, sourceRevision: number, fault = '') =>
    request<CommitResult>(`/api/migrations/${jobId}/commit`, {
      method: 'POST',
      body: JSON.stringify({ source_revision: sourceRevision }),
      headers: {
        'Content-Type': 'application/json',
        ...(fault ? { 'X-Fault-Inject': fault } : {}),
      },
    }),
  saveLegacy: (row: LegacyRow) =>
    request<{ row: LegacyRow; legacy_revision: number }>('/api/legacy', {
      method: 'POST',
      body: JSON.stringify(row),
    }),
  history: (version: number) =>
    request<{ history: HistorySummary; records: RecordRow[]; read_only: boolean }>(
      `/api/history/${version}`,
    ),
};

export const defaultMapping: MappingConfig = {
  id: { op: 'copy', source: 'legacy_id' },
  code: { op: 'trim', source: 'legacy_code' },
  name: { op: 'trim', source: 'legacy_name' },
  age: { op: 'parse_decimal', source: 'legacy_age' },
};

export const operationLabels: Record<Operation, string> = {
  copy: '复制',
  trim: '去首尾空白',
  parse_decimal: '十进制整数解析',
  constant: '常量',
};
