'use client'

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';

export default function FileUploadPage() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [progress, setProgress] = useState(0);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) {
      setError('请选择一个文件');
      return;
    }

    setLoading(true);
    setError('');
    setProgress(0);

    const formData = new FormData();
    formData.append('file', file);
    formData.append('title', title.trim());

    try {
      const xhr = new XMLHttpRequest();
      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
          setProgress(Math.round((e.loaded / e.total) * 100));
        }
      });

      xhr.onload = () => {
        if (xhr.status === 201) {
          const data = JSON.parse(xhr.responseText);
          router.push(`/documents/${data.id}`);
        } else {
          const err = JSON.parse(xhr.responseText);
          setError(err.detail || '上传失败，请重试');
          setLoading(false);
        }
      };

      xhr.onerror = () => {
        setError('网络错误，请重试');
        setLoading(false);
      };

      xhr.open('POST', '/api/files/upload', true);
      xhr.send(formData);
    } catch (err) {
      setError(String(err));
      setLoading(false);
    }
  };

  const supportedTypes = [
    { ext: '.pdf', name: 'PDF' },
    { ext: '.doc', name: 'Word DOC' },
    { ext: '.docx', name: 'Word DOCX' },
    { ext: '.xlsx', name: 'Excel XLSX' },
    { ext: '.xls', name: 'Excel XLS' },
    { ext: '.md', name: 'Markdown' },
    { ext: '.txt', name: '纯文本 TXT' },
  ];

  return (
    <main className="mx-auto max-w-4xl px-4 sm:px-6">
      <Link href="/documents" className="text-sm font-medium text-slate-500 hover:text-slate-900">← 返回知识库</Link>
      <div className="mt-5">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">文件导入</p>
        <h1 className="mt-1 text-3xl font-semibold text-slate-950">上传本地资料</h1>
        <p className="mt-2 text-sm text-slate-500">文档或表格上传后会自动进入内容解析、智能切片和向量索引流程。</p>
      </div>

      <form onSubmit={handleSubmit} className="mt-7 space-y-5 rounded-2xl border bg-white p-5 sm:p-7">
        {error && <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}

        <div>
          <label className="mb-2 block text-sm font-medium text-slate-700">
            文件
            <span className="mt-1 block text-xs font-normal text-slate-400">支持 {supportedTypes.map(t => t.name).join('、')}</span>
          </label>
          <input
            type="file"
            accept={supportedTypes.map(t => t.ext).join(',')}
            onChange={e => setFile(e.target.files?.[0] || null)}
            className="block w-full rounded-xl border border-dashed border-slate-300 bg-slate-50/70 p-4 text-sm text-slate-500 file:mr-4 file:rounded-lg file:border-0 file:bg-white file:px-4 file:py-2 file:text-sm file:font-medium file:text-slate-700 file:shadow-sm hover:border-slate-400"
            disabled={loading}
          />
        </div>

        {file && (
          <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3">
            <p className="text-sm text-emerald-800">
              已选择：{file.name}（{(file.size / 1024).toFixed(1)} KB）
            </p>
          </div>
        )}

        {loading && progress > 0 && (
          <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-200">
            <div
              className="h-2.5 rounded-full bg-slate-900 transition-[width]"
              style={{ width: `${progress}%` }}
            />
          </div>
        )}

        <div>
          <label htmlFor="title" className="mb-1.5 block text-sm font-medium text-slate-700">
            标题 <span className="font-normal text-slate-400">可留空，默认使用文件名</span>
          </label>
          <input
            id="title"
            type="text"
            value={title}
            onChange={e => setTitle(e.target.value)}
            className="w-full rounded-xl border px-3.5 py-2.5"
            placeholder={file ? file.name : '请输入标题'}
            disabled={loading}
          />
        </div>

        <div className="flex flex-wrap gap-3 border-t border-slate-100 pt-5">
          <button
            type="submit"
            disabled={loading || !file}
            className="rounded-xl bg-slate-950 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {loading ? '上传中...' : '上传'}
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
