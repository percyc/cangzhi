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
  readonly knowledgeOpen: boolean
}

interface ConsoleSource {
  getSnapshot(): ConsoleSnapshot
  subscribe(listener: () => void): () => void
}

interface ConsoleFace {
  hooks: { cangzhiConsole: ConsoleSource }
  openConsole(): void
  closeConsole(): void
  openKnowledge(): void
  closeKnowledge(): void
}

function createConsoleFace(): ConsoleFace {
  let snapshot: ConsoleSnapshot = { open: false, knowledgeOpen: false }
  const listeners = new Set<() => void>()
  const publish = (next: ConsoleSnapshot): void => {
    if (next.open === snapshot.open && next.knowledgeOpen === snapshot.knowledgeOpen) return
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
    openConsole: () => { publish({ open: true, knowledgeOpen: false }) },
    closeConsole: () => { publish({ ...snapshot, open: false }) },
    openKnowledge: () => { publish({ open: false, knowledgeOpen: true }) },
    closeKnowledge: () => { publish({ ...snapshot, knowledgeOpen: false }) },
  }
}

function CangzhiMark({ size, className }: { size: number; className?: string }) {
  return <span className={`${css.cangzhiMark} ${className ?? ''}`} style={{ width: size, height: size, fontSize: Math.max(13, size * .55) }}>知</span>
}

function CangzhiBrandName() {
  return <span className={css.cangzhiBrandName}><strong>藏知</strong><small>DSH</small></span>
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

function setWorkspaceCookie(slug: string): void {
  document.cookie = `cangzhi_workspace=${encodeURIComponent(slug)}; Path=/; Max-Age=31536000; SameSite=Lax`
}

async function syncModelWorkspace(slug: string): Promise<void> {
  const response = await fetch('/_cangzhi-plugin/workspace', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ slug }),
  })
  if (!response.ok) throw new Error(await errorMessage(response, '模型知识空间切换失败'))
  window.dispatchEvent(new CustomEvent('cangzhi-workspace-changed', { detail: { slug } }))
}

type HomeIntegrationProps = InjectFace<ConsoleFace>

function HomeIntegration({ openConsole, openKnowledge }: HomeIntegrationProps) {
  const [auth, setAuth] = useState<AuthState | null>(null)
  const [plugin, setPlugin] = useState<PluginStatus | null>(null)
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
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
    const [workspaceResponse, workspacesResponse] = await Promise.all([
      fetch(`${API}/workspaces/current`, { credentials: 'include', cache: 'no-store' }),
      fetch(`${API}/workspaces`, { credentials: 'include', cache: 'no-store' }),
    ])
    if (!workspaceResponse.ok || !workspacesResponse.ok) return
    const currentWorkspace = await workspaceResponse.json() as Workspace
    setWorkspace(currentWorkspace)
    setWorkspaces(await workspacesResponse.json() as Workspace[])
    await syncModelWorkspace(currentWorkspace.slug)
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
  const switchWorkspace = async (slug: string) => {
    const next = workspaces.find(item => item.slug === slug)
    if (next === undefined || next.slug === workspace?.slug) return
    setBusy(true); setNotice('正在切换知识空间…')
    try {
      setWorkspaceCookie(next.slug)
      await syncModelWorkspace(next.slug)
      setWorkspace(next)
      setNotice(`已切换到“${next.name}”，新会话将使用这个空间`)
      await load()
    } catch (caught) { setNotice(caught instanceof Error ? caught.message : '知识空间切换失败') }
    finally { setBusy(false) }
  }

  if (auth === null) return <section className={css.homeIntegration}><div className={css.homeLoading}>正在连接藏知知识库…</div></section>
  if (!auth.authenticated) return <section className={css.homeIntegration} data-state="login">
    <div className={css.homeIntro}><CangzhiMark size={38}/><div><strong>连接藏知知识库</strong><small>登录后，DSH 可以直接检索、引用和管理你的知识。</small></div></div>
    <form className={css.homeLogin} onSubmit={login}><input value={username} onChange={event => setUsername(event.target.value)} placeholder="藏知用户名" autoComplete="username" required/><input value={password} onChange={event => setPassword(event.target.value)} placeholder="密码" type="password" autoComplete="current-password" required/><button disabled={busy}>{busy ? '登录中…' : '登录并连接'}</button></form>
    {notice && <p className={css.homeNotice}>{notice}</p>}
  </section>
  return <section className={css.homeIntegration} data-state="ready">
    <div className={css.homeTop}><div className={css.homeIntro}><CangzhiMark size={30}/><div><strong>知识范围</strong><small>决定模型从哪些藏知资料中检索和引用</small></div></div><span className={css.homeConnection} data-ok={String(Boolean(plugin?.mcpConfigured))}>{plugin?.mcpConfigured ? '模型检索已连接' : '等待连接'}</span></div>
    <label className={css.homeWorkspace}><span>知识空间</span><div><CangzhiMark size={20}/><select value={workspace?.slug ?? 'default'} disabled={busy} onChange={event => void switchWorkspace(event.target.value)}>{workspaces.filter(item => item.status === 'active').map(item => <option key={item.id} value={item.slug}>{item.name}</option>)}</select></div><small>只控制知识检索，不改变上方运行项目的本地文件与权限</small></label>
    <div className={css.homeActions}>
      {!plugin?.mcpConfigured && <button className={css.homePrimary} disabled={busy} onClick={() => void connect()}>{busy ? '连接中…' : '启用模型检索'}</button>}
      <input ref={fileInput} type="file" accept=".pdf,.doc,.docx,.xlsx,.xls,.md,.txt" multiple hidden onChange={event => void upload(event.target.files)}/>
      <button onClick={openKnowledge}>浏览资料</button><button disabled={busy} onClick={() => fileInput.current?.click()}>上传知识</button><button onClick={openConsole}>管理知识库</button>
    </div>
    {notice && <p className={css.homeNotice}>{notice}</p>}
  </section>
}

type KnowledgeDockProps = PropsRuntime<'conversation.input.dock'> & InjectFace<ConsoleFace>

function KnowledgeDock({ openKnowledge, inputActions }: KnowledgeDockProps) {
  const [status, setStatus] = useState<PluginStatus | null>(null)
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [expanded, setExpanded] = useState(false)
  const load = async () => {
    const [statusResponse, workspaceResponse] = await Promise.all([
      fetch('/_cangzhi-plugin/status', { cache: 'no-store' }),
      fetch(`${API}/workspaces/current`, { credentials: 'include', cache: 'no-store' }),
    ])
    if (statusResponse.ok) setStatus(await statusResponse.json() as PluginStatus)
    if (workspaceResponse.ok) setWorkspace(await workspaceResponse.json() as Workspace)
  }
  useEffect(() => {
    void load()
    const update = () => { void load() }
    const useDocument = (event: Event) => {
      const detail = (event as CustomEvent<{ prompt?: string }>).detail
      if (typeof detail?.prompt === 'string') inputActions.setDraft(detail.prompt)
    }
    window.addEventListener('cangzhi-workspace-changed', update)
    window.addEventListener('cangzhi-use-document', useDocument)
    return () => {
      window.removeEventListener('cangzhi-workspace-changed', update)
      window.removeEventListener('cangzhi-use-document', useDocument)
    }
  }, [inputActions])
  const suggestions = [
    { label: '基于知识回答', prompt: '请优先检索当前藏知空间，基于找到的证据回答，并在关键结论后标注来源。\n\n' },
    { label: '总结近期资料', prompt: '请列出当前藏知空间最近更新的资料，归纳核心主题，并附上来源。' },
    { label: '对比多份资料', prompt: '请在当前藏知空间中寻找与以下主题相关的多份资料，对比它们的共同点、差异和依据：\n\n' },
  ]
  return <section className={css.knowledgeDock} data-expanded={String(expanded)}>
    <div className={css.knowledgeDockTop}><CangzhiMark size={23}/><div className={css.knowledgeDockTitle}><span><strong>知识增强</strong><i data-ok={String(Boolean(status?.mcpConfigured))}/></span><small>{workspace?.name ?? status?.activeWorkspace ?? '默认空间'} · {status?.mcpConfigured ? '模型会主动检索并引用证据' : '尚未连接模型工具'}</small></div><button className={css.dockLibraryButton} onClick={openKnowledge}>搜资料</button><button className={css.dockToggle} aria-label={expanded ? '收起知识建议' : '展开知识建议'} onClick={() => setExpanded(value => !value)}>{expanded ? '⌃' : '⌄'}</button></div>
    {expanded && <div className={css.knowledgePrompts}><span>试着这样问</span>{suggestions.map(item => <button key={item.label} disabled={!status?.mcpConfigured} onClick={() => inputActions.setDraft(item.prompt)}>{item.label}</button>)}</div>}
  </section>
}

type ConversationKnowledgeHeaderProps = PropsRuntime<'conversation.session.header.actions'> & InjectFace<ConsoleFace>

function ConversationKnowledgeHeader({ openKnowledge }: ConversationKnowledgeHeaderProps) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [current, setCurrent] = useState<Workspace | null>(null)
  const [configured, setConfigured] = useState(false)
  const [activeSlug, setActiveSlug] = useState('default')
  const [busy, setBusy] = useState(false)
  const load = async () => {
    const [statusResponse, listResponse, currentResponse] = await Promise.all([
      fetch('/_cangzhi-plugin/status', { cache: 'no-store' }),
      fetch(`${API}/workspaces`, { credentials: 'include', cache: 'no-store' }),
      fetch(`${API}/workspaces/current`, { credentials: 'include', cache: 'no-store' }),
    ])
    if (statusResponse.ok) {
      const status = await statusResponse.json() as PluginStatus
      setConfigured(status.mcpConfigured)
      setActiveSlug(status.activeWorkspace ?? 'default')
    }
    if (listResponse.ok) setWorkspaces(await listResponse.json() as Workspace[])
    if (currentResponse.ok) setCurrent(await currentResponse.json() as Workspace)
  }
  useEffect(() => {
    void load()
    const update = () => { void load() }
    window.addEventListener('cangzhi-workspace-changed', update)
    return () => window.removeEventListener('cangzhi-workspace-changed', update)
  }, [])
  const change = async (slug: string) => {
    const next = workspaces.find(item => item.slug === slug)
    if (next === undefined || next.slug === current?.slug) return
    setBusy(true)
    try { setWorkspaceCookie(slug); await syncModelWorkspace(slug); setCurrent(next) }
    finally { setBusy(false) }
  }
  if (current === null) return <button className={css.conversationKnowledgeFallback} onClick={openKnowledge}><CangzhiMark size={18}/>{configured ? `藏知 · ${activeSlug}` : '连接藏知'}</button>
  return <div className={css.conversationKnowledgeHeader} title="页面与模型工具会同步切换知识空间"><CangzhiMark size={18}/><i data-ok={String(configured)}/><span>知识空间</span><select value={current.slug} disabled={busy} onChange={event => void change(event.target.value)}>{workspaces.filter(item => item.status === 'active').map(item => <option key={item.id} value={item.slug}>{item.name}</option>)}</select><button aria-label="搜索藏知资料" onClick={openKnowledge}>⌕</button></div>
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
type Workspace = { id: number; slug: string; name: string; description?: string | null; is_default: boolean; status: 'active' | 'archived'; created_at?: string | null }
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
type PluginStatus = { mcpConfigured: boolean; toolCount: number; activeWorkspace?: string }
type Tab = 'overview' | 'search' | 'documents' | 'create' | 'upload' | 'categories' | 'spaces' | 'connect'

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
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null)
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
        const [documentResponse, categoryResponse, workspacesResponse, currentWorkspaceResponse] = await Promise.all([
          fetch(`${API}/documents/overview?limit=200&offset=0&include_processing=true`, { credentials: 'include', cache: 'no-store' }),
          fetch(`${API}/categories`, { credentials: 'include', cache: 'no-store' }),
          fetch(`${API}/workspaces`, { credentials: 'include', cache: 'no-store' }),
          fetch(`${API}/workspaces/current`, { credentials: 'include', cache: 'no-store' }),
        ])
        if (!documentResponse.ok || !categoryResponse.ok || !workspacesResponse.ok || !currentWorkspaceResponse.ok) throw new Error('知识库读取失败')
        setDocuments(await documentResponse.json() as DocumentItem[])
        setTotal(Number(documentResponse.headers.get('x-total-count') ?? 0))
        setCategories(await categoryResponse.json() as Category[])
        setWorkspaces(await workspacesResponse.json() as Workspace[])
        const current = await currentWorkspaceResponse.json() as Workspace
        setCurrentWorkspace(current)
        await syncModelWorkspace(current.slug)
      }
    } catch (caught) { setError(caught instanceof Error ? caught.message : '藏知服务不可用') }
    finally { setLoading(false) }
  }
  useEffect(() => { void refresh() }, [])
  const logout = async () => { await fetch(`${API}/auth/logout`, { method: 'POST', credentials: 'include' }); setAuth({ authenticated: false, admin: null }) }
  const switchWorkspace = async (slug: string) => {
    const next = workspaces.find(item => item.slug === slug && item.status === 'active')
    if (slug === currentWorkspace?.slug || (workspaces.some(item => item.slug === slug) && next === undefined)) return
    setLoading(true); setError('')
    try {
      setWorkspaceCookie(next.slug)
      await syncModelWorkspace(next.slug)
      setCurrentWorkspace(next ?? null)
      setTab('overview')
      await refresh()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '知识空间切换失败')
      setLoading(false)
    }
  }
  if (loading && auth === null) return <div className={css.centerState}>正在连接藏知…</div>
  if (auth !== null && !auth.authenticated) return <LoginPanel onAuthenticated={() => void refresh()} />
  return <div className={css.workspace}>
    <aside className={css.workspaceNav}>
      <div className={css.brand}><span>▤</span><div><strong>藏知</strong><small>Knowledge for DSH</small></div></div>
      <label className={css.workspaceSelector}><span>当前知识空间</span><select value={currentWorkspace?.slug ?? 'default'} disabled={loading} onChange={event => void switchWorkspace(event.target.value)}>{workspaces.filter(item => item.status === 'active').map(item => <option value={item.slug} key={item.id}>{item.name}</option>)}</select><small>{currentWorkspace?.description || '内容与模型检索在空间之间隔离'}</small></label>
      <p className={css.navSection}>知识工作</p>
      {([['overview', '⌂', '总览'], ['search', '⌕', '搜索知识'], ['documents', '▤', '资料库'], ['create', '✎', '快速收录'], ['upload', '⇧', '上传资料']] as const).map(([key, icon, label]) =>
        <button key={key} data-active={String(tab === key)} onClick={() => setTab(key)}><span>{icon}</span>{label}</button>)}
      <p className={css.navSection}>组织与连接</p>
      {([['categories', '◇', '分类管理'], ['spaces', '▦', '知识空间'], ['connect', '↗', '对话接入']] as const).map(([key, icon, label]) =>
        <button key={key} data-active={String(tab === key)} onClick={() => setTab(key)}><span>{icon}</span>{label}</button>)}
      <div className={css.connectionCard}>
        <span data-ok={String(Boolean(plugin?.mcpConfigured))} />
        <div><strong>{plugin?.mcpConfigured ? '对话检索已连接' : '管理已连接'}</strong><small>{plugin?.mcpConfigured ? `${plugin.toolCount} 个模型工具可用` : '配置 PAT 后启用模型工具'}</small></div>
      </div>
    </aside>
    <main className={css.workspaceMain}>
      <div className={css.pageHeader}><div><span className={css.pageEyebrow}>{currentWorkspace?.name ?? '知识空间'}</span><h2>{tab === 'overview' ? '知识工作台' : tab === 'search' ? '搜索知识' : tab === 'documents' ? '资料库' : tab === 'create' ? '快速收录' : tab === 'upload' ? '上传资料' : tab === 'categories' ? '分类管理' : tab === 'spaces' ? '知识空间' : '对话接入'}</h2><p>{tab === 'overview' ? `你好，${auth?.admin?.username ?? '管理员'}。从这里开始沉淀、整理和使用知识。` : currentWorkspace?.description || '当前操作仅作用于所选知识空间'}</p></div><div className={css.headerActions}><button className={css.refreshButton} disabled={loading} onClick={() => void refresh()}>{loading ? '刷新中…' : '刷新'}</button><button className={css.refreshButton} onClick={() => void logout()}>退出</button></div></div>
      {error && <div className={css.errorBanner}>{error}</div>}
      {tab === 'overview' && <Overview documents={documents} categories={categories} total={total} mcp={plugin?.mcpConfigured ?? false} workspace={currentWorkspace} go={setTab} />}
      {tab === 'search' && <KnowledgeSearch />}
      {tab === 'documents' && <Documents documents={documents} categories={categories} query={query} setQuery={setQuery} refresh={refresh} />}
      {tab === 'create' && <CreateKnowledge refresh={refresh} done={() => setTab('documents')} />}
      {tab === 'upload' && <Upload refresh={refresh} done={() => setTab('documents')} />}
      {tab === 'categories' && <Categories categories={categories} refresh={refresh} />}
      {tab === 'spaces' && <Workspaces workspaces={workspaces} current={currentWorkspace} refresh={refresh} switchTo={switchWorkspace} />}
      {tab === 'connect' && <ConversationConnect configured={plugin?.mcpConfigured ?? false} refresh={refresh} />}
    </main>
  </div>
}

function Overview({ documents, categories, total, mcp, workspace, go }: { documents: DocumentItem[]; categories: Category[]; total: number; mcp: boolean; workspace: Workspace | null; go(tab: Tab): void }) {
  const processing = documents.filter(item => ['processing', 'created', 'retry'].includes(item.pipeline?.overall_status ?? item.current_version?.processing_status ?? '')).length
  return <>
    <div className={css.stats}>
      <article><small>知识资料</small><strong>{total}</strong><span>{workspace?.name ?? '当前空间'}</span></article>
      <article><small>知识分类</small><strong>{categories.length}</strong><span>持续整理中</span></article>
      <article><small>处理队列</small><strong>{processing}</strong><span>{processing ? '后台正在处理' : '队列空闲'}</span></article>
      <article><small>模型能力</small><strong>{mcp ? '14' : '—'}</strong><span>{mcp ? '对话工具在线' : '等待 PAT 配置'}</span></article>
    </div>
    <div className={css.quickActions}>
      <button onClick={() => go('upload')}><span>⇧</span><div><strong>上传资料</strong><small>批量添加文件并自动解析</small></div><b>→</b></button>
      <button onClick={() => go('create')}><span>✎</span><div><strong>快速收录</strong><small>记录想法或收藏网页链接</small></div><b>→</b></button>
      <button onClick={() => go('search')}><span>⌕</span><div><strong>验证知识</strong><small>搜索并检查可被模型召回的内容</small></div><b>→</b></button>
    </div>
    <section className={css.panel}><div className={css.panelTitle}><div><h3>最近资料</h3><p>上传后自动解析、切片并进入检索</p></div><button onClick={() => go('upload')}>＋ 上传资料</button></div><DocumentRows documents={documents.slice(0, 8)} refresh={() => Promise.resolve()} compact /></section>
  </>
}

function statusOf(item: DocumentItem): string { return item.pipeline?.overall_status ?? item.current_version?.processing_status ?? 'created' }
const statusText: Record<string, string> = { completed: '已完成', ready: '已完成', processing: '处理中', created: '等待处理', retry: '等待重试', failed: '失败', unsupported: '未提取' }

function DocumentRows({ documents, refresh, categories = [], compact = false }: { documents: DocumentItem[]; refresh(): Promise<unknown>; categories?: Category[]; compact?: boolean }) {
  const act = async (item: DocumentItem, action: 'reprocess' | 'delete') => {
    if (action === 'delete' && !window.confirm(`将“${item.title}”移入回收站？`)) return
    const response = await fetch(`${API}/documents/${item.id}${action === 'reprocess' ? '/reprocess' : ''}`, { method: action === 'reprocess' ? 'POST' : 'DELETE', credentials: 'include' })
    if (!response.ok) window.alert(await errorMessage(response, '操作失败'))
    else await refresh()
  }
  const organize = async (item: DocumentItem, categoryId: number) => {
    const response = await fetch(`${API}/documents/${item.id}/category`, { method: 'PATCH', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ category_id: categoryId }) })
    if (!response.ok) window.alert(await errorMessage(response, '分类调整失败')); else await refresh()
  }
  if (!documents.length) return <div className={css.empty}>还没有资料，先上传第一份知识吧。</div>
  return <div className={css.documentRows}>{documents.map(item => <div className={css.documentRow} key={item.id}>
    <span className={css.fileIcon}>{item.content_kind === 'dataset' ? '▦' : '▤'}</span>
    <div className={css.documentName}><strong>{item.title}</strong><small>{item.primary_category?.name ?? '未分类'} · {new Date(item.updated_at).toLocaleString()}</small></div>
    <span className={css.status} data-status={statusOf(item)}>{statusText[statusOf(item)] ?? statusOf(item)}</span>
    {!compact && <div className={css.rowActions}>{categories.length > 0 && <select aria-label={`调整“${item.title}”的分类`} value={item.primary_category?.id ?? ''} onChange={event => { const id = Number(event.target.value); if (id > 0) void organize(item, id) }}><option value="" disabled>整理到…</option>{categories.map(category => <option value={category.id} key={category.id}>{category.name}</option>)}</select>}<button onClick={() => window.open(`${API}/documents/${item.id}/preview`, '_blank', 'noopener,noreferrer')}>预览</button><button onClick={() => void act(item, 'reprocess')}>重处理</button><button onClick={() => void act(item, 'delete')}>删除</button></div>}
  </div>)}</div>
}

function Documents({ documents, categories, query, setQuery, refresh }: { documents: DocumentItem[]; categories: Category[]; query: string; setQuery(value: string): void; refresh(): Promise<unknown> }) {
  const [categoryId, setCategoryId] = useState('')
  const [status, setStatus] = useState('')
  const visible = documents.filter(item => {
    const matchesQuery = item.title.toLowerCase().includes(query.trim().toLowerCase())
    const matchesCategory = categoryId === '' || item.primary_category?.id === Number(categoryId)
    const matchesStatus = status === '' || statusOf(item) === status || (status === 'completed' && statusOf(item) === 'ready')
    return matchesQuery && matchesCategory && matchesStatus
  })
  const hasFilters = query.trim() !== '' || categoryId !== '' || status !== ''
  return <section className={css.panel}><div className={css.libraryToolbar}><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索资料名称…"/><select value={categoryId} onChange={event => setCategoryId(event.target.value)}><option value="">全部分类</option>{categories.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select><select value={status} onChange={event => setStatus(event.target.value)}><option value="">全部状态</option><option value="completed">已完成</option><option value="processing">处理中</option><option value="created">等待处理</option><option value="retry">等待重试</option><option value="failed">失败</option></select>{hasFilters && <button onClick={() => { setQuery(''); setCategoryId(''); setStatus('') }}>清除</button>}<span>{visible.length} / {documents.length} 条</span></div><DocumentRows documents={visible} categories={categories} refresh={refresh} /></section>
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
  const [dragging, setDragging] = useState(false)
  const [progress, setProgress] = useState('')
  const supported = useMemo(() => '.pdf,.doc,.docx,.xlsx,.xls,.md,.txt', [])
  const chooseFiles = (next: File[]) => { setFiles(next.slice(0, 50)); setProgress(next.length > 50 ? '单次最多保留前 50 个文件' : '') }
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
    <input ref={input} type="file" accept={supported} multiple hidden onChange={event => chooseFiles(Array.from(event.target.files ?? []))}/>
    <button className={css.dropZone} data-dragging={String(dragging)} onClick={() => input.current?.click()} onDragEnter={event => { event.preventDefault(); setDragging(true) }} onDragOver={event => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); chooseFiles(Array.from(event.dataTransfer.files)) }} disabled={busy}><span>⇧</span><strong>{dragging ? '松开即可添加文件' : '选择或拖入知识文件'}</strong><small>PDF、Word、Excel、Markdown、TXT，单次最多 50 个</small></button>
    {files.length > 0 && <div className={css.uploadList}>{files.map((file, index) => <div key={`${file.name}:${file.size}`}><span>▤</span><strong>{file.name}</strong><small>{(file.size / 1024 / 1024).toFixed(2)} MB</small><button aria-label={`移除 ${file.name}`} disabled={busy} onClick={() => setFiles(current => current.filter((_, position) => position !== index))}>×</button></div>)}</div>}
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

function Workspaces({ workspaces, current, refresh, switchTo }: { workspaces: Workspace[]; current: Workspace | null; refresh(): Promise<unknown>; switchTo(slug: string): Promise<void> }) {
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [description, setDescription] = useState('')
  const [message, setMessage] = useState('')
  const create = async (event: React.FormEvent) => {
    event.preventDefault(); setCreating(true); setMessage('')
    const response = await fetch(`${API}/workspaces`, {
      method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name: name.trim(), slug: slug.trim().toLowerCase(), description: description.trim() || null }),
    })
    if (!response.ok) { setMessage(await errorMessage(response, '知识空间创建失败')); setCreating(false); return }
    const created = await response.json() as Workspace
    setName(''); setSlug(''); setDescription(''); setMessage(`“${created.name}”已创建，正在切换…`); setCreating(false)
    await refresh(); await switchTo(created.slug)
  }
  const edit = async (item: Workspace) => {
    const nextName = window.prompt('知识空间名称', item.name)?.trim()
    if (!nextName) return
    const nextDescription = window.prompt('空间说明（可留空）', item.description ?? '')
    if (nextDescription === null) return
    const response = await fetch(`${API}/workspaces/${encodeURIComponent(item.slug)}`, {
      method: 'PATCH', credentials: 'include', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name: nextName, description: nextDescription.trim() }),
    })
    if (!response.ok) setMessage(await errorMessage(response, '知识空间更新失败')); else await refresh()
  }
  const setArchived = async (item: Workspace, archived: boolean) => {
    if (!archived && !window.confirm(`归档“${item.name}”？空间内资料不会删除，归档后不可检索。`)) return
    const response = await fetch(`${API}/workspaces/${encodeURIComponent(item.slug)}/${archived ? 'restore' : 'archive'}`, { method: 'POST', credentials: 'include' })
    if (!response.ok) setMessage(await errorMessage(response, archived ? '恢复失败' : '归档失败')); else await refresh()
  }
  const active = workspaces.filter(item => item.status === 'active')
  const archived = workspaces.filter(item => item.status === 'archived')
  return <div className={css.spacesLayout}>
    <section className={css.spaceIntro}><div><span>▦</span><div><h3>用空间隔离不同领域的知识</h3><p>每个空间拥有独立的资料、分类和检索上下文。切换后，页面管理和 DSH 大模型会同步使用同一个空间。</p></div></div><ul><li>适合区分个人、团队或不同项目</li><li>分类用于空间内部整理，不替代空间</li><li>不需要隔离时，保留默认空间即可</li></ul></section>
    <section className={css.spacePanel}>
      <div className={css.panelTitle}><div><h3>空间列表</h3><p>{active.length} 个启用 · {archived.length} 个归档</p></div></div>
      <div className={css.spaceGrid}>{workspaces.map(item => <article key={item.id} data-current={String(item.slug === current?.slug)} data-archived={String(item.status === 'archived')}>
        <div className={css.spaceIcon}>{item.is_default ? '知' : item.name.slice(0, 1)}</div>
        <div className={css.spaceBody}><div><strong>{item.name}</strong>{item.slug === current?.slug && <span>当前</span>}{item.status === 'archived' && <span>已归档</span>}</div><p>{item.description || '暂无空间说明'}</p><small>{item.slug}</small></div>
        <div className={css.spaceActions}>{item.status === 'active' && item.slug !== current?.slug && <button onClick={() => void switchTo(item.slug)}>切换</button>}<button onClick={() => void edit(item)}>编辑</button>{!item.is_default && item.slug !== current?.slug && <button onClick={() => void setArchived(item, item.status === 'archived')}>{item.status === 'archived' ? '恢复' : '归档'}</button>}</div>
      </article>)}</div>
    </section>
    <form className={css.spaceCreate} onSubmit={create}><div><h3>新建知识空间</h3><p>空间标识创建后保持不变，建议使用简短英文，例如 product、research。</p></div><div className={css.spaceFields}><label><span>空间名称</span><input value={name} onChange={event => setName(event.target.value)} placeholder="例如：产品研发" required /></label><label><span>空间标识</span><input value={slug} onChange={event => setSlug(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))} placeholder="product" pattern="[a-z0-9][a-z0-9-]{0,63}" required /></label></div><label><span>空间说明</span><textarea value={description} onChange={event => setDescription(event.target.value)} placeholder="说明这个空间收录什么内容，帮助使用者正确选择。" /></label><button className={css.primaryButton} disabled={creating}>{creating ? '创建中…' : '创建并切换'}</button>{message && <p className={css.progressText}>{message}</p>}</form>
  </div>
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

type KnowledgeWorkbenchProps = InjectFace<ConsoleFace>
type WorkbenchDocument = { id: number; title: string; source_type: string; updated_at?: string; category?: string; snippet?: string }
type WorkbenchTab = 'browse' | 'preview' | 'context'

function openDocumentInWorkbench(id: number, title: string): void {
  window.dispatchEvent(new CustomEvent('cangzhi-open-document', { detail: { id, title } }))
}

function KnowledgeWorkbench({ useCangzhiConsole, openKnowledge, closeKnowledge, openConsole }: KnowledgeWorkbenchProps) {
  const state = useCangzhiConsole(value => value)
  const [auth, setAuth] = useState<AuthState | null>(null)
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [documents, setDocuments] = useState<WorkbenchDocument[]>([])
  const [results, setResults] = useState<WorkbenchDocument[]>([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<WorkbenchDocument | null>(null)
  const [tab, setTab] = useState<WorkbenchTab>('browse')
  const [pinned, setPinned] = useState<WorkbenchDocument[]>([])
  const [previewUrl, setPreviewUrl] = useState('')
  const [previewState, setPreviewState] = useState('选择资料后可在这里预览原文')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [width, setWidth] = useState(() => {
    const saved = Number(window.localStorage.getItem('cangzhi-workbench-width'))
    return Number.isFinite(saved) && saved >= 360 && saved <= 760 ? saved : 480
  })
  const uploadInput = useRef<HTMLInputElement>(null)
  const resizeStart = useRef({ x: 0, width: 480 })
  const widthRef = useRef(width)
  const workspaceSlugRef = useRef<string | null>(null)
  const load = async () => {
    const authResponse = await fetch(`${API}/auth/status`, { credentials: 'include', cache: 'no-store' })
    if (!authResponse.ok) throw new Error(await errorMessage(authResponse, '登录状态读取失败'))
    const authValue = await authResponse.json() as AuthState
    setAuth(authValue)
    if (!authValue.authenticated) {
      setWorkspace(null); setDocuments([]); setResults([]); setSelected(null); setPinned([]); setPreviewUrl('')
      workspaceSlugRef.current = null
      return
    }
    const [documentsResponse, workspaceResponse] = await Promise.all([
      fetch(`${API}/documents/overview?limit=60&offset=0&include_processing=true`, { credentials: 'include', cache: 'no-store' }),
      fetch(`${API}/workspaces/current`, { credentials: 'include', cache: 'no-store' }),
    ])
    if (!workspaceResponse.ok || !documentsResponse.ok) throw new Error('当前知识空间读取失败')
    const nextWorkspace = await workspaceResponse.json() as Workspace
    const workspaceChanged = workspaceSlugRef.current !== null && workspaceSlugRef.current !== nextWorkspace.slug
    workspaceSlugRef.current = nextWorkspace.slug
    setWorkspace(nextWorkspace)
    const items = await documentsResponse.json() as DocumentItem[]
    setDocuments(items.map(item => ({ id: item.id, title: item.title, source_type: item.source_type, updated_at: item.updated_at, category: item.primary_category?.name })))
    if (workspaceChanged) {
      setQuery(''); setResults([]); setSelected(null); setPinned([]); setPreviewUrl(''); setTab('browse')
      setPreviewState('选择资料后可在这里预览原文')
    }
  }
  useEffect(() => {
    if (!state.knowledgeOpen) return
    const refresh = () => { void load().catch(caught => setNotice(caught instanceof Error ? caught.message : '藏知服务不可用')) }
    refresh()
    window.addEventListener('cangzhi-workspace-changed', refresh)
    return () => window.removeEventListener('cangzhi-workspace-changed', refresh)
  }, [state.knowledgeOpen])
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl) }, [previewUrl])
  useEffect(() => {
    const frame = document.querySelector('[data-shell-overlay]')?.parentElement
    if (frame === undefined || frame === null || !state.knowledgeOpen) return
    frame.dataset.cangzhiWorkbench = 'true'
    frame.style.setProperty('--cangzhi-workbench-width', `${width}px`)
    return () => {
      delete frame.dataset.cangzhiWorkbench
      frame.style.removeProperty('--cangzhi-workbench-width')
    }
  }, [state.knowledgeOpen, width])
  const search = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setNotice('')
    const response = await fetch(`${API}/search`, { method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ query: query.trim(), limit: 40, offset: 0 }) })
    if (!response.ok) { setNotice(await errorMessage(response, '搜索失败')); setBusy(false); return }
    const body = await response.json() as { hits: SearchHit[] }
    setResults(body.hits.map(hit => ({ id: hit.document_id, title: hit.title, source_type: hit.source_type, category: hit.categories?.map(item => item.name).join('、'), snippet: hit.snippet })))
    setTab('browse')
    setBusy(false)
  }
  const preview = async (item: WorkbenchDocument) => {
    setSelected(item); setTab('preview'); setPreviewState('正在生成安全预览…'); setBusy(true)
    if (previewUrl) { URL.revokeObjectURL(previewUrl); setPreviewUrl('') }
    const response = await fetch(`${API}/documents/${item.id}/preview`, { credentials: 'include', cache: 'no-store' })
    if (!response.ok) { setPreviewState(await errorMessage(response, '这份资料暂时没有可用预览')); setBusy(false); return }
    const blob = await response.blob()
    setPreviewUrl(URL.createObjectURL(blob)); setPreviewState(''); setBusy(false)
  }
  useEffect(() => {
    const open = (event: Event) => {
      const detail = (event as CustomEvent<{ id?: number; title?: string }>).detail
      if (typeof detail?.id !== 'number') return
      const item = documents.find(document => document.id === detail.id) ?? {
        id: detail.id,
        title: detail.title?.trim() || `资料 #${detail.id}`,
        source_type: 'file',
      }
      openKnowledge()
      void preview(item)
    }
    window.addEventListener('cangzhi-open-document', open)
    return () => window.removeEventListener('cangzhi-open-document', open)
  }, [documents, openKnowledge, previewUrl])
  const useDocument = (item: WorkbenchDocument) => {
    setPinned(items => items.some(document => document.id === item.id) ? items : [...items, item])
    window.dispatchEvent(new CustomEvent('cangzhi-use-document', { detail: { prompt: `请重点读取并基于藏知资料《${item.title}》（document_id: ${item.id}）回答：\n\n` } }))
    setNotice(`已将《${item.title}》加入当前对话，问题草稿已经准备好`)
  }
  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    setBusy(true); setNotice('')
    for (const [index, file] of Array.from(files).entries()) {
      setNotice(`正在上传 ${index + 1}/${files.length}：${file.name}`)
      const body = new FormData(); body.append('file', file); body.append('title', '')
      const response = await fetch(`${API}/files/upload`, { method: 'POST', credentials: 'include', body })
      if (!response.ok) { setNotice(await errorMessage(response, `${file.name} 上传失败`)); setBusy(false); return }
    }
    setNotice('上传完成，资料正在处理'); setBusy(false); await load()
    if (uploadInput.current) uploadInput.current.value = ''
  }
  const beginResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId)
    resizeStart.current = { x: event.clientX, width }
  }
  const resize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return
    const next = Math.min(760, Math.max(360, resizeStart.current.width + resizeStart.current.x - event.clientX))
    widthRef.current = next
    setWidth(next)
  }
  const endResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return
    event.currentTarget.releasePointerCapture(event.pointerId)
    window.localStorage.setItem('cangzhi-workbench-width', String(widthRef.current))
  }
  if (!state.knowledgeOpen) return null
  const visible = results.length > 0 || query.trim() ? results : documents
  return <aside className={css.knowledgeWorkbench} aria-label="藏知工作台" style={{ width }}>
    <div className={css.workbenchResize} role="separator" aria-orientation="vertical" aria-label="调整藏知工作台宽度" aria-valuemin={360} aria-valuemax={760} aria-valuenow={width} onPointerDown={beginResize} onPointerMove={resize} onPointerUp={endResize} onPointerCancel={endResize}/>
    <header className={css.workbenchHeader}><div><CangzhiMark size={25}/><span><strong>藏知工作台</strong><small>{workspace?.name ?? '当前知识空间'}</small></span></div><div><button title="知识库管理" onClick={openConsole}>⚙</button><button title="关闭工作台" onClick={closeKnowledge}>×</button></div></header>
    <nav className={css.workbenchTabs} aria-label="藏知工作台视图"><button data-active={String(tab === 'browse')} onClick={() => setTab('browse')}>资料</button><button data-active={String(tab === 'preview')} onClick={() => setTab('preview')}>预览{selected ? ' · 1' : ''}</button><button data-active={String(tab === 'context')} onClick={() => setTab('context')}>当前对话{pinned.length > 0 ? ` · ${pinned.length}` : ''}</button></nav>
    {auth === null ? <div className={css.drawerLogin}><CangzhiMark size={44}/><h3>正在载入知识资料</h3><p>正在连接当前知识空间，请稍候。</p></div> : !auth.authenticated ? <div className={css.drawerLogin}><CangzhiMark size={44}/><h3>登录后浏览知识资料</h3><p>登录管理账户后，可以在对话旁搜索、预览和上传资料。</p><button onClick={openConsole}>前往登录</button></div> : <>
      {tab === 'browse' && <section className={css.workbenchPane}><div className={css.drawerToolbar}><form onSubmit={search}><span>⌕</span><input value={query} onChange={event => { setQuery(event.target.value); if (!event.target.value.trim()) setResults([]) }} placeholder="搜索标题、正文或知识片段"/><button disabled={busy}>{busy ? '搜索中…' : '搜索'}</button></form><input ref={uploadInput} hidden type="file" accept=".pdf,.doc,.docx,.xlsx,.xls,.md,.txt" multiple onChange={event => void upload(event.target.files)}/><button title="上传资料" onClick={() => uploadInput.current?.click()} disabled={busy}>＋</button></div><div className={css.drawerSectionTitle}><strong>{results.length > 0 || query.trim() ? '搜索结果' : '最近资料'}</strong><span>{visible.length} 项</span></div><div className={css.workbenchResults}>{visible.length === 0 ? <div className={css.drawerEmpty}>没有找到匹配的资料</div> : visible.map(item => <button key={item.id} data-selected={String(selected?.id === item.id)} onClick={() => void preview(item)}><span className={css.drawerFileIcon}>{item.source_type === 'note' ? '✎' : item.source_type === 'url' ? '↗' : '▤'}</span><div><strong>{item.title}</strong><small>{item.category || item.source_type}{item.updated_at ? ` · ${new Date(item.updated_at).toLocaleDateString()}` : ''}</small>{item.snippet && <p>{item.snippet.replace(/\s+/g, ' ').slice(0, 150)}</p>}</div></button>)}</div></section>}
      {tab === 'preview' && <section className={css.workbenchPreview}><div className={css.previewToolbar}><button onClick={() => setTab('browse')}>‹ 返回资料</button><strong title={selected?.title}>{selected?.title ?? '资料预览'}</strong>{selected && <button data-primary="true" onClick={() => useDocument(selected)}>{pinned.some(item => item.id === selected.id) ? '已加入对话' : '加入对话'}</button>}</div>{previewUrl ? <object data={previewUrl} type="application/pdf" aria-label={`${selected?.title ?? '资料'}预览`}><p>当前浏览器无法显示 PDF 预览。</p></object> : <div className={css.previewPlaceholder}><span>▤</span><p>{previewState}</p></div>}</section>}
      {tab === 'context' && <section className={css.contextPane}><div className={css.contextHero}><CangzhiMark size={34}/><div><strong>当前对话知识</strong><small>模型使用“{workspace?.name ?? '当前空间'}”，你还可以固定重点资料。</small></div></div>{pinned.length === 0 ? <div className={css.contextEmpty}>尚未固定资料。到“资料”中搜索并预览，然后点击“加入对话”。</div> : <div className={css.contextList}>{pinned.map(item => <article key={item.id}><span>▤</span><div><strong>{item.title}</strong><small>document_id: {item.id}</small></div><button onClick={() => setPinned(items => items.filter(document => document.id !== item.id))}>移除</button></article>)}</div>}<div className={css.contextTips}><strong>建议问法</strong><button onClick={() => window.dispatchEvent(new CustomEvent('cangzhi-use-document', { detail: { prompt: '请综合当前对话中固定的藏知资料，归纳共同结论、分歧与依据，并逐条标注来源。\n\n' } }))}>综合固定资料</button><button onClick={() => window.dispatchEvent(new CustomEvent('cangzhi-use-document', { detail: { prompt: '请核对当前问题与藏知资料中的原文，指出能够确认的事实、仍有疑问的部分，并标注来源。\n\n' } }))}>核对事实依据</button></div></section>}
    </>}
    {notice && <p className={css.workbenchNotice}>{notice}</p>}
    <footer className={css.workbenchStatus}><span data-ok={String(auth?.authenticated ?? false)}/><strong>{auth?.authenticated ? '知识服务在线' : '等待登录'}</strong><small>{busy ? '正在处理…' : `${documents.length} 份资料 · ${pinned.length} 份已加入对话`}</small></footer>
  </aside>
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

function nestedRecord(value: Record<string, unknown> | null): Record<string, unknown> | null {
  if (value === null) return null
  for (const key of ['structuredContent', 'result']) {
    const candidate = value[key]
    if (typeof candidate === 'object' && candidate !== null && !Array.isArray(candidate)) return candidate as Record<string, unknown>
  }
  return value
}

function EvidencePreview({ tool, value }: { tool: string; value: Record<string, unknown> | null }) {
  const payload = nestedRecord(value)
  if (payload === null) return null
  if (tool === 'knowledge_search' && Array.isArray(payload.hits)) {
    const hits = payload.hits.slice(0, 3).filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    if (!hits.length) return <div className={css.evidenceEmpty}>当前知识空间没有找到相关证据</div>
    return <div className={css.evidencePreview}><div className={css.evidenceHeading}><span>检索到 {String(payload.total ?? hits.length)} 条证据</span><small>{typeof payload.backend === 'string' ? payload.backend : 'knowledge'}</small></div>{hits.map((hit, index) => <article key={`${String(hit.document_id)}:${index}`}><span>{index + 1}</span><div><strong>{String(hit.title ?? '未命名资料')}</strong><p>{String(hit.snippet ?? hit.context ?? '').replace(/\s+/g, ' ').slice(0, 180)}</p></div>{typeof hit.document_id === 'number' && <button onClick={() => openDocumentInWorkbench(hit.document_id as number, String(hit.title ?? '未命名资料'))}>右侧预览</button>}</article>)}</div>
  }
  if (tool === 'knowledge_ask' && typeof payload.answer === 'string') {
    const citations = Array.isArray(payload.citations) ? payload.citations.filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null).slice(0, 5) : []
    return <div className={css.answerPreview}><p>{payload.answer.slice(0, 520)}</p>{citations.length > 0 && <div><span>引用 {citations.length}</span>{citations.map((citation, index) => <button key={`${String(citation.document_id ?? citation.chunk_id)}:${index}`} onClick={() => { if (typeof citation.document_id === 'number') openDocumentInWorkbench(citation.document_id, String(citation.title ?? citation.document_title ?? '知识证据')) }}><b>{index + 1}</b>{String(citation.title ?? citation.document_title ?? '知识证据')}</button>)}</div>}</div>
  }
  const preview = resultPreview(payload)
  return preview === null ? null : <p className={css.toolPreview}>{preview}</p>
}

type CangzhiToolProps = ToolCallViewProps & PropsLocale<typeof NS>

function CangzhiToolCard({ toolName, block, inspect, t }: CangzhiToolProps) {
  const rawName = toolName.startsWith(TOOL_PREFIX) ? toolName.slice(TOOL_PREFIX.length) : toolName
  const output = resultTextOf(block)
  const value = output === null ? null : parseObject(output)
  const payload = nestedRecord(value)
  const running = !('kind' in block)
  const failed = !running && block.isError
  const count = payload === null ? null : itemCount(payload)
  const argument = argumentSummary(argsRawOf(block))
  const status = running
    ? t('running')
    : failed
      ? output?.split('\n', 1)[0] ?? t('failed')
      : count === null ? t('done') : t('items', { count })
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
      {!failed && <EvidencePreview tool={rawName} value={value} />}
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

  ctx.slots.inject('conversation.hero.context', () => ctx.slots.register({
    name: 'conversation.hero.context', id: 'cangzhi-home', order: 10,
    inject: () => consoleFace,
  }, HomeIntegration))

  ctx.slots.inject('conversation.input.dock', () => ctx.slots.register({
    name: 'conversation.input.dock', id: 'cangzhi-context', order: -20,
    inject: () => consoleFace,
  }, KnowledgeDock))

  ctx.slots.inject('conversation.session.header.actions', () => ctx.slots.register({
    name: 'conversation.session.header.actions', id: 'cangzhi-knowledge-space', order: 20,
    inject: () => consoleFace,
  }, ConversationKnowledgeHeader))

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
    name: 'shell.overlay', id: 'cangzhi-knowledge-workbench', order: 90,
    inject: () => consoleFace,
  }, KnowledgeWorkbench))

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
