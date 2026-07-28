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
    { ext: '.docx', name: 'Word DOCX' },
    { ext: '.md', name: 'Markdown' },
    { ext: '.txt', name: '纯文本 TXT' },
  ];

  return (
    <main className="container mx-auto p-4">
      <div className="mb-4">
        <Link href="/documents" className="text-blue-600 hover:underline">← 返回文档列表</Link>
      </div>

      <h1 className="text-2xl font-bold mb-6">上传文件</h1>

      <form onSubmit={handleSubmit} className="max-w-xl space-y-4">
        {error && <div className="p-3 bg-red-50 text-red-600 rounded">{error}</div>}

        <div>
          <label className="block text-sm font-medium mb-1">
            文件
            <p className="text-gray-500 text-xs">支持: {supportedTypes.map(t => t.name).join(', ')}</p>
          </label>
          <input
            type="file"
            accept={supportedTypes.map(t => t.ext).join(',')}
            onChange={e => setFile(e.target.files?.[0] || null)}
            className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:bg-gray-50 file:hover:bg-gray-100"
            disabled={loading}
          />
        </div>

        {file && (
          <div className="p-3 border rounded bg-gray-50">
            <p className="text-sm">
              已选择: {file.name} ({(file.size / 1024).toFixed(1)} KB)
            </p>
          </div>
        )}

        {loading && progress > 0 && (
          <div className="w-full bg-gray-200 rounded-full h-2.5">
            <div
              className="bg-blue-600 h-2.5 rounded-full"
              style={{ width: `${progress}%` }}
            />
          </div>
        )}

        <div>
          <label htmlFor="title" className="block text-sm font-medium mb-1">
            标题 （留空使用文件名）
          </label>
          <input
            id="title"
            type="text"
            value={title}
            onChange={e => setTitle(e.target.value)}
            className="w-full px-3 py-2 border rounded"
            placeholder={file ? file.name : '请输入标题'}
            disabled={loading}
          />
        </div>

        <div className="flex gap-4">
          <button
            type="submit"
            disabled={loading || !file}
            className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50"
          >
            {loading ? '上传中...' : '上传'}
          </button>
          <Link
            href="/documents"
            className="px-4 py-2 border rounded hover:bg-gray-50"
          >
            取消
          </Link>
        </div>
      </form>
    </main>
  );
}
