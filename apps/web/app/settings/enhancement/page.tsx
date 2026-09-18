'use client';

import { useEffect, useState } from 'react';

import { withApiBasePath } from '@/lib/paths';
import { SettingsSectionNav } from '@/components/SettingsSectionNav';

type EnhancementSettings = {
  enabled: boolean;
  modules: string[];
  call_budget: number;
  cost_acknowledged: boolean;
  effective_at: string | null;
  supported_modules: string[];
};

type SaveState = 'idle' | 'saving' | 'saved' | 'error';

const MODULE_LABELS: Record<string, string> = {
  chapter: '章节理解',
  graph: '实体关系与事件',
  overview: '分层概览',
};

const emptySettings: EnhancementSettings = {
  enabled: false,
  modules: [],
  call_budget: 8,
  cost_acknowledged: false,
  effective_at: null,
  supported_modules: ['chapter', 'graph', 'overview'],
};

export default function EnhancementSettingsPage() {
  const [settings, setSettings] = useState<EnhancementSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [selectedModules, setSelectedModules] = useState<string[]>(['chapter']);
  const [callBudget, setCallBudget] = useState(8);
  const [costAcknowledged, setCostAcknowledged] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>('idle');
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(withApiBasePath('/api/enhancement/settings'), {
      cache: 'no-store',
      credentials: 'include',
    })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error('读取知识增强设置失败');
        }
        return (await response.json()) as EnhancementSettings;
      })
      .then((data) => {
        if (cancelled) return;
        const merged = { ...emptySettings, ...data };
        setSettings(merged);
        setEnabled(merged.enabled);
        const supported = new Set(merged.supported_modules);
        setSelectedModules(merged.modules.filter((m) => supported.has(m)));
        setCallBudget(merged.call_budget);
        setCostAcknowledged(merged.cost_acknowledged);
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : '读取知识增强设置失败');
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const supportedModules = settings?.supported_modules ?? ['chapter', 'graph'];
  const moduleOptions = supportedModules.filter((m) => m in MODULE_LABELS);

const toggleModule = (module: string) => {
  setSelectedModules((current) => {
    if (current.includes(module)) {
      const next = current.filter((m) => m !== module);
      // 关闭章节理解时同步关闭依赖它的分层概览
      if (module === 'chapter') return next.filter((m) => m !== 'overview');
      return next;
    }
    const next = [...current, module];
    // 分层概览依赖章节理解，开启时自动补齐章节（不静默破坏既有配置）
    if (module === 'overview' && !next.includes('chapter')) {
      return [...next, 'chapter'];
    }
    return next;
  });
};

  const handleSave = async () => {
    setSaveState('saving');
    setSaveMessage(null);
    try {
      if (!Number.isInteger(callBudget) || callBudget < 1 || callBudget > 32) {
        throw new Error('每份文档的调用预算必须在 1–32 之间');
      }
      if (enabled && !costAcknowledged) {
        throw new Error('开启知识增强前，请先确认额外模型调用费用与内容外发');
      }
      if (
        enabled &&
        selectedModules.includes('overview') &&
        !selectedModules.includes('chapter')
      ) {
        throw new Error('分层概览必须与章节理解同时开启');
      }
      const payload = {
        enabled,
        modules: enabled ? selectedModules : [],
        call_budget: callBudget,
        cost_acknowledged: enabled ? costAcknowledged : false,
      };
      const response = await fetch(withApiBasePath('/api/enhancement/settings'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || '保存知识增强设置失败');
      }
      const merged: EnhancementSettings = {
        ...emptySettings,
        ...(body as Partial<EnhancementSettings>),
      };
      setSettings(merged);
      setEnabled(merged.enabled);
      setSelectedModules(merged.modules.filter((m) => moduleOptions.includes(m)));
      setCallBudget(merged.call_budget);
      setCostAcknowledged(merged.cost_acknowledged);
      setSaveState('saved');
      setSaveMessage(enabled ? '已开启，对未来新版本生效' : '已关闭，不再自动增强');
    } catch (err) {
      setSaveState('error');
      setSaveMessage(err instanceof Error ? err.message : '保存失败');
    }
  };

  return (
    <main className="mx-auto max-w-5xl px-5 py-9">
      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
        可选的 AI 知识增强
      </p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-950">
        知识增强
      </h1>
      <p className="mt-2 text-sm leading-6 text-slate-500">
        在工作空间粒度控制可选的章节理解与实体图谱。基础摘要、分类与标签不会因此关闭。
      </p>

      <SettingsSectionNav active="enhancement" />

      {loadError ? (
        <p className="mt-6 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          读取设置失败：{loadError}
        </p>
      ) : !settings ? (
        <p className="mt-6 text-sm text-slate-500">设置加载中…</p>
      ) : (
        <form
          className="mt-6 space-y-5 rounded-2xl border-2 border-slate-200 bg-white p-5 shadow-sm"
          onSubmit={(event) => {
            event.preventDefault();
            void handleSave();
          }}
        >
          <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
            <p className="font-semibold text-slate-700">不受影响的既有能力</p>
            <p className="mt-1">
              正文提取、OCR、规则切片、全文与向量检索，以及 AI 摘要、分类和标签始终可用；
              关闭本开关不会关闭这些基础整理，也不会删除已有增强产物。
            </p>
          </div>

          <label className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 p-4">
            <span>
              <span className="block text-sm font-semibold text-slate-800">
                启用知识增强
              </span>
              <span className="mt-0.5 block text-xs text-slate-500">
                默认关闭。开启后需要额外模型调用，并可能向模型服务发送原文内容。
              </span>
            </span>
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => {
                setEnabled(event.target.checked);
                if (!event.target.checked) setCostAcknowledged(false);
              }}
              className="h-5 w-5"
            />
          </label>

          {enabled && (
            <>
              <fieldset className="rounded-xl border border-slate-200 p-4">
                <legend className="px-1 text-sm font-semibold text-slate-800">
                  增强模块
                </legend>
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  {moduleOptions.map((module) => {
                    const checked = selectedModules.includes(module);
                    return (
                      <label
                        key={module}
                        className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 p-3"
                      >
                        <span>
                          <span className="block text-sm font-medium text-slate-800">
                            {MODULE_LABELS[module] ?? module}
                          </span>
                          <span className="mt-0.5 block text-xs text-slate-500">
                            {module === 'chapter'
                              ? '按章节生成受限窗口的理解产物'
                              : module === 'graph'
                                ? '抽取实体、关系与事件，并附原文证据'
                                : '窗口收束后逐层向上做树形汇总；必须同时开启章节理解'}
                          </span>
                        </span>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleModule(module)}
                          className="h-4 w-4"
                        />
                      </label>
                    );
                  })}
                </div>
                <p className="mt-3 text-xs leading-5 text-amber-800">
                  分层概览依赖章节理解，需同时开启；它只补充树形汇总，不代表全文已完整理解，
                  同名实体在各窗口保持独立身份，不做自动合并。本批仅补充知识与图谱，
                  辅助切片的原子替换（保留基线、候选校验通过后<strong>原子启用</strong>并支持回滚）
                  尚未开放，本页面不提供该选项。
                </p>
              </fieldset>

              <label className="block rounded-xl border border-slate-200 p-4">
                <span className="block text-sm font-semibold text-slate-800">
                  每份文档的模型调用预算（1–32）
                </span>
                <span className="mt-0.5 block text-xs text-slate-500">
                  窗口分析与逐层树形汇总共用同一份预算；重试也计入预算，预算不是 token 账单。
                </span>
                <input
                  type="number"
                  min={1}
                  max={32}
                  value={callBudget}
                  onChange={(event) =>
                    setCallBudget(Number(event.target.value) || 8)
                  }
                  className="mt-2 rounded border border-slate-300 px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
                />
              </label>

              <label className="mt-1 flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50/50 p-4">
                <input
                  type="checkbox"
                  checked={costAcknowledged}
                  onChange={(event) => setCostAcknowledged(event.target.checked)}
                  className="mt-0.5 h-4 w-4"
                />
                <span className="text-sm leading-6 text-amber-900">
                  <span className="font-semibold">
                    我确认开启后会新增额外的模型调用，并把不可信原文内容发送给已配置的模型服务。
                  </span>
                  <span className="mt-0.5 block text-xs text-amber-800">
                    开关只作用于生效时间之后的新版本；历史资料不会被自动处理，需在文档页手动发起。
                  </span>
                </span>
              </label>
            </>
          )}

          <div className="mt-4 rounded-lg bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-500">
            <p>
              关闭开关只停止未来的自动增强，不会删除、重切或切换当前索引；运行中的任务保留明确取消入口。
            </p>
          </div>

          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={saveState === 'saving'}
              className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
            >
              {saveState === 'saving' ? '保存中…' : '保存设置'}
            </button>
            {saveMessage && (
              <p
                className={`text-sm ${
                  saveState === 'error' ? 'text-red-700' : 'text-emerald-700'
                }`}
                role={saveState === 'error' ? 'alert' : 'status'}
              >
                {saveMessage}
              </p>
            )}
          </div>
        </form>
      )}
    </main>
  );
}
