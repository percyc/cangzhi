'use client';

export default function ListPagination({ page, pageSize, total, loading = false, onPageChange, onPageSizeChange }: {
  page: number; pageSize: number; total: number; loading?: boolean;
  onPageChange: (page: number) => void; onPageSizeChange?: (size: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(Math.max(1, page), pages);
  const button = 'rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm disabled:opacity-40';
  return <nav aria-label="列表分页" className="list-pagination">
    <span role="status" className="text-sm text-slate-600">
      {total ? `第 ${(current - 1) * pageSize + 1}–${Math.min(current * pageSize, total)} 条 / 共 ${total} 条` : '共 0 条'}
      {loading && ' · 加载中…'}
    </span>
    <div className="flex flex-wrap items-center gap-2">
      {onPageSizeChange && <label className="text-sm text-slate-600">每页 <select aria-label="每页条数" value={pageSize} disabled={loading}
        onChange={event => onPageSizeChange(Number(event.target.value))} className="rounded-lg border px-2 py-2">
        {[12, 24, 48].map(size => <option key={size} value={size}>{size} 条</option>)}
      </select></label>}
      <button type="button" className={button} disabled={loading || current <= 1} onClick={() => onPageChange(current - 1)}>上一页</button>
      <form key={current} className="flex items-center gap-1 text-sm" onSubmit={event => {
        event.preventDefault();
        const input = new FormData(event.currentTarget).get('page');
        const value = Number(input);
        if (Number.isInteger(value) && value >= 1 && value <= pages && value !== current && !loading) onPageChange(value);
      }}>
        <input aria-label="跳转页码" name="page" type="number" min={1} max={pages} defaultValue={current} disabled={loading || pages <= 1}
          className="w-16 rounded-lg border px-2 py-2" />
        <span>/ {pages} 页</span>
        <button type="submit" disabled={loading || pages <= 1} className={button}>跳转</button>
      </form>
      <button type="button" className={button} disabled={loading || current >= pages} onClick={() => onPageChange(current + 1)}>下一页</button>
    </div>
  </nav>;
}
