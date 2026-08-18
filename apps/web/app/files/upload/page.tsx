'use client'

import { useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';

type FileStatus = 'pending' | 'uploading' | 'done' | 'error';

type UploadItem = {
  key: string;
  file: File;
  status: FileStatus;
  progress: number;
  documentId?: number;
  error?: string;
};

const MAX_FILES = 50;
const UPLOAD_TIMEOUT_MS = 5 * 60 * 1000;

const SUPPORTED_TYPES = [
  { ext: '.pdf', name: 'PDF' },
  { ext: '.doc', name: 'Word DOC' },
  { ext: '.docx', name: 'Word DOCX' },
  { ext: '.xlsx', name: 'Excel XLSX' },
  { ext: '.xls', name: 'Excel XLS' },
  { ext: '.md', name: 'Markdown' },
  { ext: '.txt', name: '纯文本 TXT' },
];

const ACCEPT = SUPPORTED_TYPES.map((t) => t.ext).join(',');

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function isSupported(name: string): boolean {
  const extension = name.slice(name.lastIndexOf('.')).toLowerCase();
  return SUPPORTED_TYPES.some((t) => t.ext === extension);
}

export default function FileUploadPage() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const keyCounter = useRef(0);

  const [items, setItems] = useState<UploadItem[]>([]);
  const [title, setTitle] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadPosition, setUploadPosition] = useState({ current: 0, total: 0 });
  const [globalError, setGlobalError] = useState('');
  const [finished, setFinished] = useState(false);

  const singleFileMode = items.length === 1;
  const pendingOrFailedCount = items.filter(
    (item) => item.status === 'pending' || item.status === 'error',
  ).length;
  const overallProgress =
    items.length === 0
      ? 0
      : Math.round(
          items.reduce((sum, item) => sum + item.progress, 0) / items.length,
        );

  const addFiles = (files: FileList | null) => {
    if (!files || files.length === 0 || uploading) return;

    const added = Array.from(files);
    const seenKeys = new Set(
      items.map(
        (item) =>
          `${item.file.name}|${item.file.size}|${item.file.lastModified}`,
      ),
    );
    const unsupported: File[] = [];
    const duplicates: File[] = [];
    const candidates: File[] = [];
    for (const file of added) {
      if (!isSupported(file.name)) {
        unsupported.push(file);
        continue;
      }
      const signature = `${file.name}|${file.size}|${file.lastModified}`;
      if (seenKeys.has(signature)) {
        duplicates.push(file);
        continue;
      }
      seenKeys.add(signature);
      candidates.push(file);
    }
    const room = Math.max(0, MAX_FILES - items.length);
    const accepted = candidates.slice(0, room);
    const overflow = candidates.slice(room);
    const newItems: UploadItem[] = accepted.map((file) => {
      keyCounter.current += 1;
      return {
        key: String(keyCounter.current),
        file,
        status: 'pending',
        progress: 0,
      };
    });
    setItems((prev) => [...prev, ...newItems]);

    const notices: string[] = [];
    if (unsupported.length > 0) {
      notices.push(
        `不支持的类型已跳过：${unsupported.map((file) => file.name).join('、')}`,
      );
    }
    if (duplicates.length > 0) {
      notices.push(`已跳过 ${duplicates.length} 个重复文件`);
    }
    if (overflow.length > 0) {
      notices.push(`最多保留 ${MAX_FILES} 个文件，另有 ${overflow.length} 个未加入`);
    }
    setGlobalError(notices.join('；'));
    setFinished(false);
    if (inputRef.current) {
      inputRef.current.value = '';
    }
  };

  const removeItem = (key: string) => {
    if (uploading) return;
    setItems((prev) => prev.filter((item) => item.key !== key));
    setFinished(false);
  };

  const uploadOne = (
    key: string,
    file: File,
    titleForFile: string,
  ): Promise<number> => {
    return new Promise((resolve, reject) => {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('title', titleForFile);

      const xhr = new XMLHttpRequest();
      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) {
          const percent = Math.round((event.loaded / event.total) * 100);
          setItems((prev) =>
            prev.map((item) =>
              item.key === key ? { ...item, progress: percent } : item,
            ),
          );
        }
      });
      xhr.onload = () => {
        if (xhr.status === 201) {
          try {
            const data = JSON.parse(xhr.responseText);
            resolve(data.id as number);
          } catch {
            reject(new Error('上传响应解析失败'));
          }
        } else {
          let message = '上传失败，请重试';
          try {
            const data = JSON.parse(xhr.responseText);
            if (data && typeof data.detail === 'string') {
              message = data.detail;
            }
          } catch {
            // keep the default message
          }
          reject(new Error(message));
        }
      };
      xhr.onerror = () => reject(new Error('网络错误，请重试'));
      xhr.ontimeout = () => reject(new Error('上传超时，请重试'));
      xhr.open('POST', '/api/files/upload', true);
      xhr.timeout = UPLOAD_TIMEOUT_MS;
      xhr.send(formData);
    });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const targets = items.filter(
      (item) => item.status === 'pending' || item.status === 'error',
    );
    if (targets.length === 0) {
      setGlobalError('请选择至少一个待上传的文件');
      return;
    }

    setUploading(true);
    setUploadPosition({ current: 1, total: targets.length });
    setGlobalError('');
    setFinished(false);

    let allSucceeded = true;
    let singleDocumentId: number | null = null;

    const isSingleFileRun = items.length === 1;
    for (const [index, target] of targets.entries()) {
      setUploadPosition({ current: index + 1, total: targets.length });
      const titleForFile = isSingleFileRun ? title.trim() : '';
      setItems((prev) =>
        prev.map((item) =>
          item.key === target.key
            ? { ...item, status: 'uploading', progress: 0, error: undefined }
            : item,
        ),
      );
      try {
        const documentId = await uploadOne(target.key, target.file, titleForFile);
        if (isSingleFileRun) {
          singleDocumentId = documentId;
        }
        setItems((prev) =>
          prev.map((item) =>
            item.key === target.key
              ? { ...item, status: 'done', progress: 100, documentId }
              : item,
          ),
        );
      } catch (err) {
        allSucceeded = false;
        setItems((prev) =>
          prev.map((item) =>
            item.key === target.key
              ? {
                  ...item,
                  status: 'error',
                  error:
                    err instanceof Error ? err.message : '上传失败，请重试',
                }
              : item,
          ),
        );
      }
    }

    setUploading(false);
    setUploadPosition({ current: 0, total: 0 });
    if (isSingleFileRun && allSucceeded && singleDocumentId != null) {
      router.push(`/documents/${singleDocumentId}`);
      return;
    }
    setFinished(true);
  };

  const removeFinished = () => {
    if (uploading) return;
    setItems((prev) => prev.filter((item) => item.status !== 'done'));
    setFinished(false);
    setGlobalError('');
  };

  const doneCount = items.filter((item) => item.status === 'done').length;
  const failedCount = items.filter((item) => item.status === 'error').length;

  return (
    <main className="mx-auto max-w-4xl px-4 sm:px-6">
      <Link href="/documents" className="text-sm font-medium text-slate-500 hover:text-slate-900">← 返回知识库</Link>
      <div className="mt-5">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">文件导入</p>
        <h1 className="mt-1 text-3xl font-semibold text-slate-950">上传本地资料</h1>
        <p className="mt-2 text-sm text-slate-500">可一次选择多个文件，上传后会自动进入内容解析、智能切片和向量索引流程。</p>
      </div>

      <form onSubmit={handleSubmit} className="mt-7 space-y-5 rounded-2xl border bg-white p-5 sm:p-7">
        {globalError && <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{globalError}</div>}

        <div>
          <label className="mb-2 block text-sm font-medium text-slate-700">
            文件
            <span className="mt-1 block text-xs font-normal text-slate-400">
              支持 {SUPPORTED_TYPES.map((t) => t.name).join('、')}，最多 {MAX_FILES} 个
            </span>
          </label>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={ACCEPT}
            onChange={(e) => addFiles(e.target.files)}
            className="block w-full rounded-xl border border-dashed border-slate-300 bg-slate-50/70 p-4 text-sm text-slate-500 file:mr-4 file:rounded-lg file:border-0 file:bg-white file:px-4 file:py-2 file:text-sm file:font-medium file:text-slate-700 file:shadow-sm hover:border-slate-400"
            disabled={uploading}
          />
        </div>

        {items.length === 0 ? (
          <p className="text-sm text-slate-400">尚未选择文件，请点击上方按钮选择本地资料。</p>
        ) : (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <p className="text-sm font-medium text-slate-700">
                已选择 {items.length} 个文件
                {uploading && (
                  <span className="ml-2 text-xs font-normal text-slate-400">
                    正在上传第 {uploadPosition.current} / {uploadPosition.total} 个
                  </span>
                )}
              </p>
              {!uploading && items.some((item) => item.status === 'done') && (
                <button
                  type="button"
                  onClick={removeFinished}
                  className="text-xs font-medium text-slate-500 hover:text-slate-800"
                >
                  移除已成功项
                </button>
              )}
            </div>
            <ul className="divide-y divide-slate-100 overflow-hidden rounded-xl border border-slate-200">
              {items.map((item) => (
                <li key={item.key} className="flex items-center gap-3 px-4 py-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium text-slate-800">{item.file.name}</span>
                      <span className="shrink-0 text-xs text-slate-400">{formatSize(item.file.size)}</span>
                    </div>
                    {item.status === 'uploading' && (
                      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
                        <div
                          className="h-1.5 rounded-full bg-slate-900 transition-[width]"
                          style={{ width: `${item.progress}%` }}
                        />
                      </div>
                    )}
                    {item.status === 'error' && item.error && (
                      <p className="mt-1 text-xs text-red-600">{item.error}</p>
                    )}
                  </div>
                  <StatusBadge item={item} />
                  {!uploading && item.status !== 'done' && (
                    <button
                      type="button"
                      onClick={() => removeItem(item.key)}
                      aria-label={`移除 ${item.file.name}`}
                      className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                    >
                      <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden><path d="M6 6l8 8M14 6l-8 8" /></svg>
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        {uploading && (
          <div>
            <div className="mb-1 flex items-center justify-between text-xs text-slate-500">
              <span>上传进度</span>
              <span>{overallProgress}%</span>
            </div>
            <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-200">
              <div
                className="h-2.5 rounded-full bg-slate-900 transition-[width]"
                style={{ width: `${overallProgress}%` }}
              />
            </div>
          </div>
        )}

        {finished && (
          <div className={`rounded-xl border p-4 ${failedCount > 0 ? 'border-amber-200 bg-amber-50' : 'border-emerald-200 bg-emerald-50'}`}>
            {failedCount === 0 ? (
              <p className="text-sm font-medium text-emerald-800">
                ✓ 全部 {doneCount} 个文件上传成功，正在后台解析
              </p>
            ) : (
              <p className="text-sm font-medium text-amber-800">
                上传完成：成功 {doneCount} 个，失败 {failedCount} 个
              </p>
            )}
            <div className="mt-3 flex flex-wrap gap-2">
              <Link
                href="/documents"
                className="rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
              >
                查看知识库
              </Link>
              {failedCount > 0 && (
                <button
                  type="button"
                  onClick={() => setFinished(false)}
                  className="rounded-xl border border-amber-300 px-4 py-2 text-sm font-medium text-amber-800 hover:bg-amber-100"
                >
                  重试失败的文件
                </button>
              )}
              {doneCount > 0 && (
                <button
                  type="button"
                  onClick={removeFinished}
                  className="rounded-xl border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
                >
                  继续上传
                </button>
              )}
            </div>
          </div>
        )}

        <div>
          <label htmlFor="title" className="mb-1.5 block text-sm font-medium text-slate-700">
            标题 <span className="font-normal text-slate-400">{singleFileMode ? '可留空，默认使用文件名' : '批量上传时每个文件使用自己的文件名'}</span>
          </label>
          <input
            id="title"
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full rounded-xl border px-3.5 py-2.5"
            placeholder={singleFileMode && items[0] ? items[0].file.name : '请输入标题'}
            disabled={uploading || !singleFileMode}
          />
        </div>

        <div className="flex flex-wrap gap-3 border-t border-slate-100 pt-5">
          <button
            type="submit"
            disabled={uploading || pendingOrFailedCount === 0}
            className="rounded-xl bg-slate-950 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {uploading
              ? `上传中 ${overallProgress}%...`
              : pendingOrFailedCount === 0
                ? '全部已上传'
                : pendingOrFailedCount > 1
                  ? `批量上传 ${pendingOrFailedCount} 个文件`
                  : '上传'}
          </button>
          <Link
            href="/documents"
            className="rounded-xl border border-slate-300 px-5 py-2.5 text-sm text-slate-700 hover:bg-slate-50"
          >
            取消
          </Link>
        </div>
      </form>
    </main>
  );
}

function StatusBadge({ item }: { item: UploadItem }) {
  if (item.status === 'pending') {
    return <span className="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500">待上传</span>;
  }
  if (item.status === 'uploading') {
    return <span className="shrink-0 rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-medium text-indigo-700">{item.progress}%</span>;
  }
  if (item.status === 'done' && item.documentId != null) {
    return (
      <Link
        href={`/documents/${item.documentId}`}
        className="shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700 hover:bg-emerald-200"
      >
        ✓ 已上传
      </Link>
    );
  }
  return <span className="shrink-0 rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">失败</span>;
}
