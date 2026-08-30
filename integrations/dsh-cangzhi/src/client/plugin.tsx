/** Browser half: full Cangzhi console plus replay-stable MCP tool cards. */

import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type { InjectFace, PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type { ToolCallViewProps } from '@deepseek-ai/dsh-client-ui-tool/client'
import type {} from '@deepseek-ai/dsh-client-locale/client'
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { useEffect, useMemo, useRef, useState } from 'react'
import css from './Cangzhi.module.css'

const NS = 'cangzhi'
const API = '/_dsh-cangzhi-api'
const TOOL_PREFIX = 'mcp__cangzhi__'

const RAW_TOOLS = [
  'knowledge_list_scopes',
  'knowledge_list_facets',
  'knowledge_list_documents',
  'knowledge_search',
  'knowledge_ask',
  'knowledge_get_document',
  'knowledge_get_chunk',
  'knowledge_list_datasets',
  'knowledge_get_dataset_schema',
  'knowledge_preview_dataset_rows',
  'knowledge_query_dataset',
  'knowledge_get_evidence_by_chunk',
  'knowledge_get_evidence_by_dataset',
  'knowledge_preview_evidence_rows',
] as const

const zh = {
  footer: '藏知',
  consoleTitle: '藏知管理中心',
  close: '关闭',
  frameHint: 'DSH 原生知识工作台',
  running: '执行中…',
  done: '已完成',
  failed: '调用失败',
  items: '{{count}} 项',
  inspect: '检查调用',
  details: '查看原始结果',
  tools: {
    knowledge_list_scopes: '知识范围',
    knowledge_list_facets: '知识分类',
    knowledge_list_documents: '文档列表',
    knowledge_search: '知识搜索',
    knowledge_ask: '知识问答',
    knowledge_get_document: '读取文档',
    knowledge_get_chunk: '读取片段',
    knowledge_list_datasets: '数据集列表',
    knowledge_get_dataset_schema: '数据集结构',
    knowledge_preview_dataset_rows: '预览数据集',
    knowledge_query_dataset: '查询数据集',
    knowledge_get_evidence_by_chunk: '片段证据',
    knowledge_get_evidence_by_dataset: '数据集证据',
    knowledge_preview_evidence_rows: '预览证据',
  },
}

const en = {
  footer: '藏知',
  consoleTitle: '藏知管理中心',
  close: 'Close',
  frameHint: 'Native knowledge workspace for DSH',
  running: 'Running…',
  done: 'Completed',
  failed: 'Call failed',
  items: '{{count}} items',
  inspect: 'Inspect call',
  details: 'Show raw result',
  tools: {
    knowledge_list_scopes: 'Knowledge scopes',
    knowledge_list_facets: 'Knowledge facets',
    knowledge_list_documents: 'Documents',
    knowledge_search: 'Knowledge search',
    knowledge_ask: 'Knowledge answer',
    knowledge_get_document: 'Read document',
    knowledge_get_chunk: 'Read chunk',
    knowledge_list_datasets: 'Datasets',
    knowledge_get_dataset_schema: 'Dataset schema',
    knowledge_preview_dataset_rows: 'Preview dataset',
    knowledge_query_dataset: 'Query dataset',
    knowledge_get_evidence_by_chunk: 'Chunk evidence',
    knowledge_get_evidence_by_dataset: 'Dataset evidence',
    knowledge_preview_evidence_rows: 'Preview evidence',
  },
}

interface ConsoleSnapshot {
  readonly open: boolean
}

interface ConsoleSource {
  getSnapshot(): ConsoleSnapshot
  subscribe(listener: () => void): () => void
}

interface ConsoleFace {
  hooks: { cangzhiConsole: ConsoleSource }
  openConsole(): void
  closeConsole(): void
}

function createConsoleFace(): ConsoleFace {
  let snapshot: ConsoleSnapshot = { open: false }
  const listeners = new Set<() => void>()
  const publish = (next: ConsoleSnapshot): void => {
    if (next.open === snapshot.open) return
    snapshot = next
    for (const listener of listeners) listener()
  }
  const source: ConsoleSource = {
    getSnapshot: () => snapshot,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
  }
  return {
    hooks: { cangzhiConsole: source },
    openConsole: () => { publish({ ...snapshot, open: true }) },
    closeConsole: () => { publish({ ...snapshot, open: false }) },
  }
}

function CangzhiMark({ size, className }: { size: number; className?: string }) {
  return <span className={`${css.cangzhiMark} ${className ?? ''}`} style={{ width: size, height: size, fontSize: Math.max(13, size * .55) }}>知</span>
}

function CangzhiBrandName() {
  return <span className={css.cangzhiBrandName}><strong>藏知</strong><small>DSH</small></span>
}

function CangzhiHeadline() {
  return <>和你的知识聊聊</>
}

function CangzhiDocumentTitle() {
  useEffect(() => {
    const update = () => {
      const next = document.title
        .replace(/DSH Local Build$/u, '藏知 DSH')
        .replace(/DSH 本地构建$/u, '藏知 DSH')
      if (next !== document.title) document.title = next
    }
    update()
    const observer = new MutationObserver(update)
    observer.observe(document.head, { childList: true, subtree: true, characterData: true })
    return () => { observer.disconnect() }
  }, [])
  return null
}

type HomeIntegrationProps = InjectFace<ConsoleFace>

function HomeIntegration({ openConsole }: HomeIntegrationProps) {
  const [auth, setAuth] = useState<AuthState | null>(null)
  const [plugin, setPlugin] = useState<PluginStatus | null>(null)
  const [stats, setStats] = useState({ documents: 0, categories: 0, processing: 0 })
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)

  const load = async () => {
    const [authResponse, pluginResponse] = await Promise.all([
      fetch(`${API}/auth/status`, { credentials: 'include', cache: 'no-store' }),
      fetch('/_cangzhi-plugin/status', { cache: 'no-store' }),
    ])
    const authValue = await authResponse.json() as AuthState
    setAuth(authValue)
    if (pluginResponse.ok) setPlugin(await pluginResponse.json() as PluginStatus)
    if (!authValue.authenticated) return
    const [documentsResponse, categoriesResponse] = await Promise.all([
      fetch(`${API}/documents/overview?limit=200&offset=0&include_processing=true`, { credentials: 'include', cache: 'no-store' }),
      fetch(`${API}/categories`, { credentials: 'include', cache: 'no-store' }),
    ])
    if (!documentsResponse.ok || !categoriesResponse.ok) return
    const documents = await documentsResponse.json() as DocumentItem[]
    const categories = await categoriesResponse.json() as Category[]
    setStats({
      documents: Number(documentsResponse.headers.get('x-total-count') ?? documents.length),
      categories: categories.length,
      processing: documents.filter(item => ['processing', 'created', 'retry'].includes(statusOf(item))).length,
    })
  }
  useEffect(() => { void load().catch(() => { setNotice('藏知服务暂时不可用') }) }, [])

  const login = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setNotice('')
    const response = await fetch(`${API}/auth/login`, { method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ username, password }) })
    if (!response.ok) { setNotice(await errorMessage(response, '登录失败')); setBusy(false); return }
    setPassword('')
    if (plugin?.mcpConfigured) { setNotice('登录成功，藏知对话已经连接'); await load(); setBusy(false); return }
    await connect()
  }
  const connect = async () => {
    setBusy(true); setNotice('正在创建 DSH 专用访问令牌…')
    const response = await fetch(`${API}/access-tokens`, { method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: 'DSH 对话插件', scopes: ['knowledge:read', 'knowledge:search', 'knowledge:ask'] }) })
    if (!response.ok) { setNotice(await errorMessage(response, '令牌创建失败')); setBusy(false); return }
    const { token } = await response.json() as { token: string }
    const setup = await fetch('/_cangzhi-plugin/token', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token }) })
    setNotice(setup.ok ? '连接成功，藏知工具正在自动上线' : await errorMessage(setup, 'DSH 凭据写入失败'))
    setBusy(false); await load()
  }
  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    setBusy(true)
    for (const [index, file] of Array.from(files).entries()) {
      setNotice(`正在上传 ${index + 1}/${files.length}：${file.name}`)
      const body = new FormData(); body.append('file', file); body.append('title', '')
      const response = await fetch(`${API}/files/upload`, { method: 'POST', credentials: 'include', body })
      if (!response.ok) { setNotice(await errorMessage(response, `${file.name} 上传失败`)); setBusy(false); return }
    }
    setNotice('上传完成，已进入知识处理队列'); setBusy(false); await load()
  }

  if (auth === null) return <section className={css.homeIntegration}><div className={css.homeLoading}>正在连接藏知知识库…</div></section>
  if (!auth.authenticated) return <section className={css.homeIntegration} data-state="login">
    <div className={css.homeIntro}><CangzhiMark size={38}/><div><strong>连接藏知知识库</strong><small>登录后，DSH 可以直接检索、引用和管理你的知识。</small></div></div>
    <form className={css.homeLogin} onSubmit={login}><input value={username} onChange={event => setUsername(event.target.value)} placeholder="藏知用户名" autoComplete="username" required/><input value={password} onChange={event => setPassword(event.target.value)} placeholder="密码" type="password" autoComplete="current-password" required/><button disabled={busy}>{busy ? '登录中…' : '登录并连接'}</button></form>
    {notice && <p className={css.homeNotice}>{notice}</p>}
  </section>
  return <section className={css.homeIntegration} data-state="ready">
    <div className={css.homeTop}><div className={css.homeIntro}><CangzhiMark size={38}/><div><strong>你的藏知知识库</strong><small>{plugin?.mcpConfigured ? '已接入当前 DSH 对话' : '管理服务已连接，尚未启用模型检索'}</small></div></div><span className={css.homeConnection} data-ok={String(Boolean(plugin?.mcpConfigured))}>{plugin?.mcpConfigured ? '对话已连接' : '等待认证'}</span></div>
    <div className={css.homeStats}><span><strong>{stats.documents}</strong> 份资料</span><span><strong>{stats.categories}</strong> 个分类</span><span><strong>{stats.processing}</strong> 项处理中</span></div>
    <div className={css.homeActions}>
      {!plugin?.mcpConfigured && <button className={css.homePrimary} disabled={busy} onClick={() => void connect()}>{busy ? '连接中…' : '启用模型检索'}</button>}
      <input ref={fileInput} type="file" accept=".pdf,.doc,.docx,.xlsx,.xls,.md,.txt" multiple hidden onChange={event => void upload(event.target.files)}/>
      <button disabled={busy} onClick={() => fileInput.current?.click()}>上传知识</button><button onClick={openConsole}>管理知识库</button>
    </div>
    {notice && <p className={css.homeNotice}>{notice}</p>}
  </section>
}

function KnowledgeDock({ openConsole }: HomeIntegrationProps) {
  const [status, setStatus] = useState<PluginStatus | null>(null)
  useEffect(() => { void fetch('/_cangzhi-plugin/status', { cache: 'no-store' }).then(async response => { if (response.ok) setStatus(await response.json() as PluginStatus) }) }, [])
  return <div className={css.knowledgeDock}>
    <CangzhiMark size={22}/><span><strong>藏知</strong>{status?.mcpConfigured ? '已连接，可在对话中检索知识库' : '尚未认证，返回新对话首页完成连接'}</span><button onClick={openConsole}>知识库</button>
  </div>
}

type ConsoleActionProps = PropsRuntime<'sidebar.footer.action'>
  & InjectFace<ConsoleFace> & PropsLocale<typeof NS>

function ConsoleAction({ wide, useCangzhiConsole, openConsole, t }: ConsoleActionProps) {
  const state = useCangzhiConsole(value => value)
  return (
    <div className={wide ? css.footer : `${css.footer} ${css.footerRail}`}>
      <button
        type="button"
        className={css.footerButton}
        data-active={String(state.open)}
        aria-label={t('consoleTitle')}
        title={t('consoleTitle')}
        onClick={openConsole}
      >
        <span className={css.bookIcon} aria-hidden>▤</span>
        {wide && <span className={css.footerLabel}>{t('footer')}</span>}
      </button>
    </div>
  )
}

type AuthState = { authenticated: boolean; setup_required?: boolean; admin?: { username: string } | null }
type Category = { id: number; name: string; slug: string; description?: string | null; document_count: number; is_default: boolean }
type DocumentItem = {
  id: number
  title: string
  source_type: string
  content_kind: string
  updated_at: string
  primary_category?: { id: number; name: string } | null
  current_version?: { processing_status: string } | null
  pipeline?: { overall_status: string } | null
}
type PluginStatus = { mcpConfigured: boolean; toolCount: number }
type Tab = 'overview' | 'search' | 'documents' | 'create' | 'upload' | 'categories' | 'connect'

async function errorMessage(response: Response, fallback: string): Promise<string> {
  const value = await response.json().catch(() => null) as { detail?: string | { message?: string } } | null
  if (typeof value?.detail === 'string') return value.detail
  if (typeof value?.detail === 'object' && typeof value.detail.message === 'string') return value.detail.message
  return fallback
}

function LoginPanel({ onAuthenticated }: { onAuthenticated(): void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true); setError('')
    const response = await fetch(`${API}/auth/login`, {
      method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    setBusy(false)
    if (!response.ok) { setError(await errorMessage(response, '登录失败')); return }
    onAuthenticated()
  }
  return <div className={css.loginPanel}>
    <div className={css.loginMark}>▤</div>
    <h2>登录藏知</h2>
    <p>登录后即可在 DSH 内上传、管理和维护知识库。</p>
    <form onSubmit={submit} className={css.loginForm}>
      <input value={username} onChange={event => setUsername(event.target.value)} placeholder="用户名" autoComplete="username" required />
      <input value={password} onChange={event => setPassword(event.target.value)} placeholder="密码" type="password" autoComplete="current-password" required />
      {error && <div className={css.errorBanner}>{error}</div>}
      <button disabled={busy}>{busy ? '登录中…' : '登录'}</button>
    </form>
  </div>
}

function NativeWorkspace() {
  const [auth, setAuth] = useState<AuthState | null>(null)
  const [plugin, setPlugin] = useState<PluginStatus | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [documents, setDocuments] = useState<DocumentItem[]>([])
  const [categories, setCategories] = useState<Category[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const refresh = async () => {
    setLoading(true); setError('')
    try {
      const authResponse = await fetch(`${API}/auth/status`, { credentials: 'include', cache: 'no-store' })
      const authValue = await authResponse.json() as AuthState
      setAuth(authValue)
      const pluginResponse = await fetch('/_cangzhi-plugin/status', { cache: 'no-store' })
      if (pluginResponse.ok) setPlugin(await pluginResponse.json() as PluginStatus)
      if (authValue.authenticated) {
        const [documentResponse, categoryResponse] = await Promise.all([
          fetch(`${API}/documents/overview?limit=200&offset=0&include_processing=true`, { credentials: 'include', cache: 'no-store' }),
          fetch(`${API}/categories`, { credentials: 'include', cache: 'no-store' }),
        ])
        if (!documentResponse.ok || !categoryResponse.ok) throw new Error('知识库读取失败')
        setDocuments(await documentResponse.json() as DocumentItem[])
        setTotal(Number(documentResponse.headers.get('x-total-count') ?? 0))
        setCategories(await categoryResponse.json() as Category[])
      }
    } catch (caught) { setError(caught instanceof Error ? caught.message : '藏知服务不可用') }
    finally { setLoading(false) }
  }
  useEffect(() => { void refresh() }, [])
  const logout = async () => { await fetch(`${API}/auth/logout`, { method: 'POST', credentials: 'include' }); setAuth({ authenticated: false, admin: null }) }
  if (loading && auth === null) return <div className={css.centerState}>正在连接藏知…</div>
  if (auth !== null && !auth.authenticated) return <LoginPanel onAuthenticated={() => void refresh()} />
  const visible = documents.filter(item => item.title.toLowerCase().includes(query.trim().toLowerCase()))
  return <div className={css.workspace}>
    <aside className={css.workspaceNav}>
      <div className={css.brand}><span>▤</span><div><strong>藏知</strong><small>Knowledge for DSH</small></div></div>
      {([['overview', '总览'], ['search', '搜索知识'], ['documents', '知识库'], ['create', '链接与随手记'], ['upload', '上传资料'], ['categories', '分类管理'], ['connect', '对话接入']] as const).map(([key, label]) =>
        <button key={key} data-active={String(tab === key)} onClick={() => setTab(key)}>{label}</button>)}
      <div className={css.connectionCard}>
        <span data-ok={String(Boolean(plugin?.mcpConfigured))} />
        <div><strong>{plugin?.mcpConfigured ? '对话检索已连接' : '管理已连接'}</strong><small>{plugin?.mcpConfigured ? `${plugin.toolCount} 个模型工具可用` : '配置 PAT 后启用模型工具'}</small></div>
      </div>
    </aside>
    <main className={css.workspaceMain}>
      <div className={css.pageHeader}><div><h2>{tab === 'overview' ? '知识工作台' : tab === 'search' ? '搜索知识' : tab === 'documents' ? '知识库' : tab === 'create' ? '链接与随手记' : tab === 'upload' ? '上传资料' : tab === 'categories' ? '分类管理' : '对话接入'}</h2><p>你好，{auth?.admin?.username ?? '管理员'}</p></div><div className={css.headerActions}><button className={css.refreshButton} onClick={() => void refresh()}>刷新</button><button className={css.refreshButton} onClick={() => void logout()}>退出</button></div></div>
      {error && <div className={css.errorBanner}>{error}</div>}
      {tab === 'overview' && <Overview documents={documents} categories={categories} total={total} mcp={plugin?.mcpConfigured ?? false} go={setTab} />}
      {tab === 'search' && <KnowledgeSearch />}
      {tab === 'documents' && <Documents documents={visible} query={query} setQuery={setQuery} refresh={refresh} />}
      {tab === 'create' && <CreateKnowledge refresh={refresh} done={() => setTab('documents')} />}
      {tab === 'upload' && <Upload refresh={refresh} done={() => setTab('documents')} />}
      {tab === 'categories' && <Categories categories={categories} refresh={refresh} />}
      {tab === 'connect' && <ConversationConnect configured={plugin?.mcpConfigured ?? false} refresh={refresh} />}
    </main>
  </div>
}

function Overview({ documents, categories, total, mcp, go }: { documents: DocumentItem[]; categories: Category[]; total: number; mcp: boolean; go(tab: Tab): void }) {
  const processing = documents.filter(item => ['processing', 'created', 'retry'].includes(item.pipeline?.overall_status ?? item.current_version?.processing_status ?? '')).length
  return <>
    <div className={css.stats}>
      <article><small>知识资料</small><strong>{total}</strong><span>当前工作区</span></article>
      <article><small>知识分类</small><strong>{categories.length}</strong><span>持续整理中</span></article>
      <article><small>处理队列</small><strong>{processing}</strong><span>{processing ? '后台正在处理' : '队列空闲'}</span></article>
      <article><small>模型能力</small><strong>{mcp ? '14' : '—'}</strong><span>{mcp ? '对话工具在线' : '等待 PAT 配置'}</span></article>
    </div>
    <section className={css.panel}><div className={css.panelTitle}><div><h3>最近资料</h3><p>上传后自动解析、切片并进入检索</p></div><button onClick={() => go('upload')}>＋ 上传资料</button></div><DocumentRows documents={documents.slice(0, 8)} refresh={() => Promise.resolve()} compact /></section>
  </>
}

function statusOf(item: DocumentItem): string { return item.pipeline?.overall_status ?? item.current_version?.processing_status ?? 'created' }
const statusText: Record<string, string> = { completed: '已完成', ready: '已完成', processing: '处理中', created: '等待处理', retry: '等待重试', failed: '失败', unsupported: '未提取' }

function DocumentRows({ documents, refresh, compact = false }: { documents: DocumentItem[]; refresh(): Promise<unknown>; compact?: boolean }) {
  const act = async (item: DocumentItem, action: 'reprocess' | 'delete') => {
    if (action === 'delete' && !window.confirm(`将“${item.title}”移入回收站？`)) return
    const response = await fetch(`${API}/documents/${item.id}${action === 'reprocess' ? '/reprocess' : ''}`, { method: action === 'reprocess' ? 'POST' : 'DELETE', credentials: 'include' })
    if (!response.ok) window.alert(await errorMessage(response, '操作失败'))
    else await refresh()
  }
  if (!documents.length) return <div className={css.empty}>还没有资料，先上传第一份知识吧。</div>
  return <div className={css.documentRows}>{documents.map(item => <div className={css.documentRow} key={item.id}>
    <span className={css.fileIcon}>{item.content_kind === 'dataset' ? '▦' : '▤'}</span>
    <div className={css.documentName}><strong>{item.title}</strong><small>{item.primary_category?.name ?? '未分类'} · {new Date(item.updated_at).toLocaleString()}</small></div>
    <span className={css.status} data-status={statusOf(item)}>{statusText[statusOf(item)] ?? statusOf(item)}</span>
    {!compact && <div className={css.rowActions}><button onClick={() => void act(item, 'reprocess')}>重新处理</button><button onClick={() => void act(item, 'delete')}>删除</button></div>}
  </div>)}</div>
}

function Documents({ documents, query, setQuery, refresh }: { documents: DocumentItem[]; query: string; setQuery(value: string): void; refresh(): Promise<unknown> }) {
  return <section className={css.panel}><div className={css.libraryToolbar}><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索资料名称…"/><span>{documents.length} 条资料</span></div><DocumentRows documents={documents} refresh={refresh} /></section>
}

type SearchHit = { document_id: number; title: string; source_type: string; score: number; snippet: string; categories?: Array<{ name: string }> }

function KnowledgeSearch() {
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<SearchHit[]>([])
  const [backend, setBackend] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const search = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError('')
    const response = await fetch(`${API}/search`, { method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ query: query.trim(), limit: 30, offset: 0 }) })
    if (!response.ok) { setError(await errorMessage(response, '搜索失败')); setBusy(false); return }
    const body = await response.json() as { hits: SearchHit[]; backend: string }
    setHits(body.hits); setBackend(body.backend); setBusy(false)
  }
  return <section className={css.panel}>
    <form className={css.searchForm} onSubmit={search}><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索标题、正文或知识片段" required/><button disabled={busy}>{busy ? '搜索中…' : '搜索'}</button></form>
    {error && <div className={css.errorBanner}>{error}</div>}
    {backend && <p className={css.searchMeta}>{hits.length} 个结果 · {backend}</p>}
    <div className={css.searchResults}>{hits.map((hit, index) => <article key={`${hit.document_id}:${index}`}><div><strong>{hit.title}</strong><small>{hit.source_type} · 匹配度 {(hit.score * 100).toFixed(0)}% {hit.categories?.map(item => `· ${item.name}`).join('')}</small></div><p>{hit.snippet}</p></article>)}</div>
    {!busy && backend && !hits.length && <div className={css.empty}>没有找到匹配的知识。</div>}
  </section>
}

function CreateKnowledge({ refresh, done }: { refresh(): Promise<unknown>; done(): void }) {
  const [mode, setMode] = useState<'note' | 'url'>('note')
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setMessage('')
    const response = await fetch(mode === 'note' ? `${API}/notes` : `${API}/sources/url`, {
      method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(mode === 'note' ? { title, content } : { url }),
    })
    if (!response.ok) { setMessage(await errorMessage(response, '保存失败')); setBusy(false); return }
    setMessage(mode === 'note' ? '随手记已保存并进入处理队列' : '链接已收录并进入抓取队列')
    setTitle(''); setContent(''); setUrl(''); setBusy(false); await refresh(); window.setTimeout(done, 650)
  }
  return <section className={css.createPanel}>
    <div className={css.modeTabs}><button data-active={String(mode === 'note')} onClick={() => setMode('note')}>随手记</button><button data-active={String(mode === 'url')} onClick={() => setMode('url')}>网页链接</button></div>
    <form onSubmit={submit}>
      {mode === 'note' ? <><input value={title} onChange={event => setTitle(event.target.value)} placeholder="标题（可选）"/><textarea value={content} onChange={event => setContent(event.target.value)} placeholder="记录想法、会议要点或任何需要沉淀的知识…" required/></> : <input value={url} onChange={event => setUrl(event.target.value)} placeholder="https://example.com/article" type="url" required/>}
      <button className={css.primaryButton} disabled={busy}>{busy ? '保存中…' : mode === 'note' ? '保存随手记' : '收录链接'}</button>
      {message && <p className={css.progressText}>{message}</p>}
    </form>
  </section>
}

function Upload({ refresh, done }: { refresh(): Promise<unknown>; done(): void }) {
  const input = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState('')
  const supported = useMemo(() => '.pdf,.doc,.docx,.xlsx,.xls,.md,.txt', [])
  const upload = async () => {
    setBusy(true)
    for (let index = 0; index < files.length; index += 1) {
      setProgress(`正在上传 ${index + 1} / ${files.length}：${files[index]!.name}`)
      const body = new FormData(); body.append('file', files[index]!); body.append('title', '')
      const response = await fetch(`${API}/files/upload`, { method: 'POST', credentials: 'include', body })
      if (!response.ok) { window.alert(await errorMessage(response, `${files[index]!.name} 上传失败`)); setBusy(false); return }
    }
    setProgress('上传完成，资料已进入处理队列'); setFiles([]); setBusy(false); await refresh(); window.setTimeout(done, 700)
  }
  return <section className={css.uploadPanel}>
    <input ref={input} type="file" accept={supported} multiple hidden onChange={event => setFiles(Array.from(event.target.files ?? []))}/>
    <button className={css.dropZone} onClick={() => input.current?.click()} disabled={busy}><span>⇧</span><strong>选择或拖入知识文件</strong><small>PDF、Word、Excel、Markdown、TXT，单次最多 50 个</small></button>
    {files.length > 0 && <div className={css.uploadList}>{files.map(file => <div key={`${file.name}:${file.size}`}><span>▤</span><strong>{file.name}</strong><small>{(file.size / 1024 / 1024).toFixed(2)} MB</small></div>)}</div>}
    {progress && <p className={css.progressText}>{progress}</p>}
    <button className={css.primaryButton} disabled={!files.length || busy} onClick={() => void upload()}>{busy ? '上传中…' : `上传 ${files.length || ''} 个文件`}</button>
  </section>
}

function Categories({ categories, refresh }: { categories: Category[]; refresh(): Promise<unknown> }) {
  const [name, setName] = useState('')
  const create = async (event: React.FormEvent) => { event.preventDefault(); const response = await fetch(`${API}/categories`, { method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name }) }); if (!response.ok) window.alert(await errorMessage(response, '创建失败')); else { setName(''); await refresh() } }
  const rename = async (item: Category) => { const next = window.prompt('新的分类名称', item.name)?.trim(); if (!next || next === item.name) return; const response = await fetch(`${API}/categories/${item.id}`, { method: 'PATCH', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: next }) }); if (!response.ok) window.alert(await errorMessage(response, '重命名失败')); else await refresh() }
  const remove = async (item: Category) => { if (!window.confirm(`删除分类“${item.name}”？`)) return; const response = await fetch(`${API}/categories/${item.id}`, { method: 'DELETE', credentials: 'include' }); if (!response.ok) window.alert(await errorMessage(response, '删除失败')); else await refresh() }
  return <><form className={css.categoryForm} onSubmit={create}><input value={name} onChange={event => setName(event.target.value)} placeholder="新分类名称" required/><button>新增分类</button></form><section className={css.categoryGrid}>{categories.map(item => <article key={item.id}><span>▤</span><div><strong>{item.name}</strong><small>{item.document_count} 条资料 · {item.slug}</small></div><button onClick={() => void rename(item)}>重命名</button>{!item.is_default && <button onClick={() => void remove(item)}>删除</button>}</article>)}</section></>
}

function ConversationConnect({ configured, refresh }: { configured: boolean; refresh(): Promise<unknown> }) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [tokens, setTokens] = useState<Array<{ id: number; name: string; token_prefix: string; scopes: string[]; revoked_at: string | null; created_at: string }>>([])
  const loadTokens = async () => {
    const response = await fetch(`${API}/access-tokens`, { credentials: 'include', cache: 'no-store' })
    if (response.ok) setTokens((await response.json() as { items: typeof tokens }).items)
  }
  useEffect(() => { void loadTokens() }, [])
  const connect = async () => {
    setBusy(true); setMessage('正在创建最小权限访问令牌…')
    const tokenResponse = await fetch(`${API}/access-tokens`, {
      method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name: 'DSH 对话插件', scopes: ['knowledge:read', 'knowledge:search', 'knowledge:ask'] }),
    })
    if (!tokenResponse.ok) { setMessage(await errorMessage(tokenResponse, '访问令牌创建失败')); setBusy(false); return }
    const created = await tokenResponse.json() as { token: string }
    setMessage('正在安全写入 DSH 凭据存储…')
    const setupResponse = await fetch('/_cangzhi-plugin/token', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: created.token }),
    })
    if (!setupResponse.ok) { setMessage(await errorMessage(setupResponse, 'DSH 凭据写入失败')); setBusy(false); return }
    setMessage('连接成功，模型工具将在数秒内自动上线。'); setBusy(false); await Promise.all([refresh(), loadTokens()])
  }
  const disconnect = async () => {
    if (!window.confirm('断开 DSH 与藏知的对话连接？知识管理功能仍然可用。')) return
    setBusy(true)
    const response = await fetch('/_cangzhi-plugin/token', { method: 'DELETE' })
    setMessage(response.ok ? '已断开 DSH 对话连接' : await errorMessage(response, '断开失败'))
    setBusy(false); await refresh()
  }
  const revoke = async (id: number) => {
    if (!window.confirm('撤销这个藏知访问令牌？使用它的客户端会立即失效。')) return
    const response = await fetch(`${API}/access-tokens/${id}/revoke`, { method: 'POST', credentials: 'include' })
    if (!response.ok) setMessage(await errorMessage(response, '撤销失败'))
    await loadTokens()
  }
  return <section className={css.connectPanel}>
    <div className={css.connectHero}><span data-ok={String(configured)}>{configured ? '✓' : '↗'}</span><div><h3>{configured ? 'DSH 对话已连接藏知' : '让大模型直接调用藏知'}</h3><p>{configured ? '知识搜索、问答、文档读取和数据集查询工具已经注册到对话。' : '点击一次即可创建只读/检索/问答权限的独立令牌，并安全保存到 DSH 凭据存储。'}</p></div></div>
    <div className={css.capabilityGrid}><article><strong>知识检索</strong><small>按范围、分类和文档召回证据</small></article><article><strong>知识问答</strong><small>基于藏知内容生成带引用答案</small></article><article><strong>数据集查询</strong><small>查看结构、预览并精确查询表格</small></article></div>
    {!configured ? <button className={css.primaryButton} disabled={busy} onClick={() => void connect()}>{busy ? '正在连接…' : '启用 DSH 对话能力'}</button> : <button className={css.secondaryButton} disabled={busy} onClick={() => void disconnect()}>断开对话连接</button>}
    {message && <p className={css.progressText}>{message}</p>}
    <p className={css.securityNote}>令牌仅在创建时从藏知传入 DSH，本页面不会显示或回读密钥；可随时在藏知的访问令牌管理中撤销。</p>
    <div className={css.tokenList}><h4>藏知访问令牌</h4>{tokens.length === 0 ? <p>暂无访问令牌</p> : tokens.map(token => <div key={token.id}><div><strong>{token.name}</strong><small>{token.token_prefix}… · {token.scopes.join('、')}</small></div><span data-revoked={String(Boolean(token.revoked_at))}>{token.revoked_at ? '已撤销' : '有效'}</span>{!token.revoked_at && <button onClick={() => void revoke(token.id)}>撤销</button>}</div>)}</div>
  </section>
}

type ConsoleOverlayProps = InjectFace<ConsoleFace> & PropsLocale<typeof NS>

function ConsoleOverlay({ useCangzhiConsole, closeConsole, t }: ConsoleOverlayProps) {
  const state = useCangzhiConsole(value => value)
  if (!state.open) return null
  return (
    <section className={css.overlay} role="dialog" aria-modal="true" aria-label={t('consoleTitle')}>
      <header className={css.consoleHeader}>
        <strong className={css.consoleTitle}>{t('consoleTitle')}</strong>
        <span className={css.nativeBadge}>{t('frameHint')}</span>
        <button type="button" className={css.closeButton} aria-label={t('close')} title={t('close')} onClick={closeConsole}>×</button>
      </header>
      <NativeWorkspace />
    </section>
  )
}

function argsRawOf(block: ToolCallViewProps['block']): string {
  return ('kind' in block ? block.call?.argsRaw : block.argsRaw) ?? ''
}

function resultTextOf(block: ToolCallViewProps['block']): string | null {
  if (!('kind' in block)) return null
  const text = block.content
    .map(item => item.type === 'text' ? item.text : JSON.stringify(item))
    .join('\n')
  if (text.length > 0) return text
  if (block.error === undefined) return null
  return `${block.error.name}: ${block.error.code}`
}

function parseObject(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text)
    return typeof value === 'object' && value !== null && !Array.isArray(value)
      ? value as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

function argumentSummary(raw: string): string | null {
  const args = parseObject(raw)
  if (args === null) return raw.trim().slice(0, 160) || null
  for (const key of ['question', 'query', 'sql', 'document_id', 'chunk_id', 'dataset_id', 'scope']) {
    const value = args[key]
    if (typeof value === 'string' && value.length > 0) return value.slice(0, 160)
  }
  return null
}

function itemCount(value: unknown): number | null {
  if (Array.isArray(value)) return value.length
  if (typeof value !== 'object' || value === null) return null
  const object = value as Record<string, unknown>
  for (const key of ['results', 'hits', 'documents', 'chunks', 'datasets', 'rows', 'items', 'scopes', 'facets', 'evidence']) {
    if (Array.isArray(object[key])) return object[key].length
  }
  for (const key of ['count', 'total']) {
    if (typeof object[key] === 'number') return object[key]
  }
  return null
}

function resultPreview(value: Record<string, unknown> | null): string | null {
  if (value === null) return null
  for (const key of ['answer', 'summary', 'title', 'name', 'content', 'text']) {
    const candidate = value[key]
    if (typeof candidate === 'string' && candidate.length > 0) return candidate.slice(0, 360)
  }
  return null
}

type CangzhiToolProps = ToolCallViewProps & PropsLocale<typeof NS>

function CangzhiToolCard({ toolName, block, inspect, t }: CangzhiToolProps) {
  const rawName = toolName.startsWith(TOOL_PREFIX) ? toolName.slice(TOOL_PREFIX.length) : toolName
  const output = resultTextOf(block)
  const value = output === null ? null : parseObject(output)
  const running = !('kind' in block)
  const failed = !running && block.isError
  const count = value === null ? null : itemCount(value)
  const argument = argumentSummary(argsRawOf(block))
  const status = running
    ? t('running')
    : failed
      ? output?.split('\n', 1)[0] ?? t('failed')
      : count === null ? t('done') : t('items', { count })
  const preview = failed ? null : resultPreview(value)
  const title = t(`tools.${rawName}` as never)
  return (
    <div className={css.toolCard} data-tool={toolName} data-state={failed ? 'error' : running ? 'running' : 'ok'}>
      <div className={css.toolRow}>
        <span className={css.toolGlyph} aria-hidden>▤</span>
        <span className={css.toolTitle}>{title}</span>
        <span className={css.toolSummary}>{argument === null ? status : `${argument} · ${status}`}</span>
        {inspect !== undefined && (
          <button type="button" className={css.inspectButton} onClick={inspect} title={t('inspect')}>↗</button>
        )}
      </div>
      {preview !== null && <p className={css.toolPreview}>{preview}</p>}
      {output !== null && (
        <details className={css.toolDetails}>
          <summary>{t('details')}</summary>
          <pre className={css.toolOutput}>{output.slice(0, 12000)}</pre>
        </details>
      )}
    </div>
  )
}

export const inject = ['slots', 'locale']

export function apply(ctx: ClientContext): void {
  ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'cangzhi: dictionaries')
  const consoleFace = createConsoleFace()

  ctx.slots.inject('sidebar.brand.mark', () => ctx.slots.register({
    name: 'sidebar.brand.mark', id: 'cangzhi-brand-mark', order: 0,
  }, CangzhiMark))

  ctx.slots.inject('sidebar.brand.name', () => ctx.slots.register({
    name: 'sidebar.brand.name', id: 'cangzhi-brand-name', order: 0,
  }, CangzhiBrandName))

  ctx.slots.inject('conversation.hero.brand.mark', () => ctx.slots.register({
    name: 'conversation.hero.brand.mark', id: 'cangzhi-hero-mark', order: 0,
  }, CangzhiMark))

  ctx.slots.inject('conversation.hero.headline', () => ctx.slots.register({
    name: 'conversation.hero.headline', id: 'cangzhi-headline', order: 0,
  }, CangzhiHeadline))

  ctx.slots.inject('conversation.hero.extension', () => ctx.slots.register({
    name: 'conversation.hero.extension', id: 'cangzhi-home', order: 10,
    inject: () => consoleFace,
  }, HomeIntegration))

  ctx.slots.inject('conversation.input.dock', () => ctx.slots.register({
    name: 'conversation.input.dock', id: 'cangzhi-context', order: -20,
    inject: () => consoleFace,
  }, KnowledgeDock))

  ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({
    name: 'sidebar.footer.action',
    id: 'cangzhi-console',
    order: 40,
    locale: NS,
    inject: () => consoleFace,
  }, ConsoleAction))

  ctx.slots.inject('shell.overlay', () => ctx.slots.register({
    name: 'shell.overlay',
    id: 'cangzhi-console',
    order: 100,
    locale: NS,
    inject: () => consoleFace,
  }, ConsoleOverlay))

  ctx.slots.inject('shell.overlay', () => ctx.slots.register({
    name: 'shell.overlay', id: 'cangzhi-document-title', order: -100,
  }, CangzhiDocumentTitle))

  ctx.slots.inject('tool.call.toolview', function* () {
    for (const rawName of RAW_TOOLS) {
      yield ctx.slots.register({
        name: 'tool.call.toolview',
        key: `${TOOL_PREFIX}${rawName}`,
        locale: NS,
      }, CangzhiToolCard)
    }
  })
}
