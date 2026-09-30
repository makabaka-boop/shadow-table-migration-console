// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from './App';

const previewRows = [
  {
    row_num: 1,
    legacy_id: 1,
    values: { id: 1, code: 'A100', name: 'Ada Lovelace', age: 36, source_revision: 3 },
    sources: {
      id: '复制 legacy_id',
      code: '去首尾空白 legacy_code',
      name: '去首尾空白 legacy_name',
      age: '十进制整数 legacy_age',
    },
    status: 'valid' as const,
    errors: [],
  },
  {
    row_num: 2,
    legacy_id: 3,
    values: { id: 3, code: 'A100', name: 'Duplicate Code', age: 42, source_revision: 3 },
    sources: {
      id: '复制 legacy_id',
      code: '去首尾空白 legacy_code',
      name: '去首尾空白 legacy_name',
      age: '十进制整数 legacy_age',
    },
    status: 'invalid' as const,
    errors: [{ field: 'code', code: 'unique_violation', message: "code='A100' 重复", source: '去首尾空白 legacy_code' }],
  },
];

const baseState = {
  meta: { legacy_revision: 3, target_revision: 0, active_table: 'records' },
  legacy_records: [],
  records: [],
  history: [],
};

const mocks = vi.hoisted(() => ({
  state: vi.fn(),
  preview: vi.fn(),
  commit: vi.fn(),
  saveLegacy: vi.fn(),
  history: vi.fn(),
}));

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api');
  return {
    ...actual,
    api: {
      state: mocks.state,
      preview: mocks.preview,
      commit: mocks.commit,
      saveLegacy: mocks.saveLegacy,
      history: mocks.history,
    },
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.state.mockResolvedValue(baseState);
  mocks.preview.mockResolvedValue({
    job_id: 'job-1',
    ok: false,
    source_revision: 3,
    target_revision: 0,
    total_rows: 2,
    failed_rows: [previewRows[1]],
    candidate_rows: previewRows,
  });
});

afterEach(() => {
  cleanup();
});

describe('App migration report', () => {
  it('shows revisions, row status and mapping provenance for failed preview', async () => {
    render(<App />);

    expect(await screen.findByText('源表修订')).toBeInTheDocument();
    expect(screen.getByText('3', { selector: 'strong' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '复制到影子表并预演' }));

    expect(await screen.findByText('预演发现 1 个失败行；正式表未改变')).toBeInTheDocument();
    expect(screen.getByTestId('status-1')).toHaveTextContent('通过');
    expect(screen.getByTestId('status-2')).toHaveTextContent('失败');
    expect(screen.getByTestId('source-2-code')).toHaveTextContent('去首尾空白 legacy_code');
    expect(screen.getByText("code='A100' 重复")).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /提交并携带源表修订号/ })).toBeDisabled();
  });

  it('blocks commit and asks for recomputation when source revision changed after preview', async () => {
    mocks.state
      .mockResolvedValueOnce(baseState)
      .mockResolvedValueOnce({ ...baseState, meta: { ...baseState.meta, legacy_revision: 4 } });
    render(<App />);

    await screen.findByText('源表修订');
    fireEvent.click(screen.getByRole('button', { name: '复制到影子表并预演' }));
    await screen.findByText('预演发现 1 个失败行；正式表未改变');

    await waitFor(() => expect(screen.getByText(/当前源表修订 4/)).toBeInTheDocument());
    expect(screen.getByText(/必须重新预演/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /提交并携带源表修订号/ })).toBeDisabled();
    expect(mocks.commit).not.toHaveBeenCalled();
  });
});
