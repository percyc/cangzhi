'use client'

import Link from 'next/link'
import { useParams, useRouter } from 'next/navigation'
import { FormEvent, useEffect, useState } from 'react'

export default function EditNotePage() {
  const params = useParams<{ id: string }>()
  const router = useRouter()
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    fetch(`/api/documents/${params.id}`)
      .then((response) => {
        if (!response.ok) throw new Error('无法读取这条随手记')
        return response.json()
      })
      .then((document) => {
        if (document.source_type !== 'note') throw new Error('这条资料不是随手记')
        setTitle(document.title)
        setContent(document.current_version?.raw_content || '')
      })
      .catch((reason) => setError(String(reason.message || reason)))
      .finally(() => setLoading(false))
  }, [params.id])

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      const response = await fetch(`/api/notes/${params.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content }),
      })
      if (!response.ok) throw new Error('保存失败，请稍后重试')
      router.push(`/documents/${params.id}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
      setSaving(false)
    }
  }

  if (loading) return <main className="mx-auto max-w-3xl p-6">加载中…</main>

  return (
    <main className="mx-auto max-w-3xl p-6">
      <Link href={`/documents/${params.id}`} className="text-blue-700">← 返回资料</Link>
      <h1 className="mb-6 mt-4 text-2xl font-semibold">编辑随手记</h1>
      <form onSubmit={save} className="space-y-4">
        {error && <p className="rounded bg-red-50 p-3 text-red-700">{error}</p>}
        <input
          aria-label="标题"
          className="w-full rounded-lg border border-slate-300 px-4 py-3"
          maxLength={1024}
          onChange={(event) => setTitle(event.target.value)}
          value={title}
        />
        <textarea
          aria-label="内容"
          className="min-h-[360px] w-full rounded-lg border border-slate-300 px-4 py-3"
          onChange={(event) => setContent(event.target.value)}
          required
          value={content}
        />
        <button
          className="rounded-lg bg-slate-900 px-5 py-3 text-white disabled:opacity-50"
          disabled={saving}
          type="submit"
        >
          {saving ? '保存中…' : '保存修改'}
        </button>
      </form>
    </main>
  )
}
