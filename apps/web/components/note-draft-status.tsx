'use client';

import { DraftWriteStatus } from '@/lib/note-drafts';

export function NoteDraftStatus({ status, restoredAt, notice, canDiscard, onDiscard }: {
  status: DraftWriteStatus;
  restoredAt: number | null;
  notice?: string;
  canDiscard: boolean;
  onDiscard: () => void;
}) {
  const unsafe = status === 'unavailable' || status === 'too-large' || status === 'expired';
  return (
    <div className={`rounded-xl border px-4 py-3 text-sm ${unsafe ? 'border-amber-200 bg-amber-50 text-amber-900' : 'border-slate-200 bg-slate-50 text-slate-600'}`}>
      <p role="status" aria-live="polite">
        {restoredAt !== null && <span className="font-medium">已恢复本标签页 {new Date(restoredAt).toLocaleString('zh-CN')} 暂存的草稿。 </span>}
        {status === 'saved' ? '草稿已暂存，尚未保存到知识库。'
          : status === 'too-large' ? '正文超过浏览器草稿上限（512 KiB），最新修改尚未暂存。离开前请保存或复制正文。'
            : status === 'unavailable' ? '浏览器暂存不可用，最新修改仅保留在此页。离开前请保存或复制正文。'
              : status === 'expired' ? '暂存草稿已超过 24 小时。当前正文仍在此页，离开前请保存，或继续编辑以重新暂存。'
              : '输入时自动暂存草稿。'}
      </p>
      <p className="mt-1 text-xs opacity-80">仅限当前标签页、当前空间，24 小时内可恢复；不跨设备同步，不是正式备份。关闭标签页可能清除草稿。</p>
      {notice && <p className="mt-1 text-xs">{notice}</p>}
      {canDiscard && <button type="button" className="mt-2 text-xs font-medium underline underline-offset-2" onClick={onDiscard}>丢弃草稿</button>}
    </div>
  );
}
