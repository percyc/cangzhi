/* eslint-disable @typescript-eslint/no-require-imports -- Execute isolated TS/React handlers with synthetic settings. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const vm = require('node:vm');

function load(relative, mocks) {
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '..', relative), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  const testModule = { exports: {} };
  vm.runInThisContext(`(function(require,module,exports,window){${code}\n})`)(
    id => mocks[id] || require(id), testModule, testModule.exports,
    { location: { search: '', hash: '' }, addEventListener() {}, removeEventListener() {}, confirm: () => true },
  );
  return testModule.exports;
}

const setup = load('lib/settings-setup.ts', {});
const initialConfig = {
  provider: 'openai', openai_base_url: 'https://saved.example/v1', openai_model: 'chat-original',
  has_api_key: true, ollama_base_url: 'http://localhost:11434', ollama_model: null,
  timeout_seconds: 30, prompt_version: 'test', updated_at: null,
  embedding_provider: 'disabled', embedding_base_url: null, embedding_model: null,
  has_embedding_api_key: false, embedding_timeout_seconds: 30,
  ocr_provider: 'disabled', ocr_base_url: null, ocr_model: null, has_ocr_api_key: false,
  ocr_timeout_seconds: 30, ocr_confidence_threshold: 600, ocr_min_chars: 8, ocr_max_external_pages: 20,
};
const noIndex = { active_profile: { id: null }, profiles: [] };

function children(node) {
  if (Array.isArray(node)) return node.flatMap(children);
  if (!node || typeof node !== 'object') return [];
  return [node, ...children(node.props?.children)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join('');
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node !== 'object') return String(node);
  return text(node.props?.children);
}

async function renderSettings(overrides = {}, customApi = {}) {
  const state = [], refs = [], effects = [], queue = [];
  let stateCursor = 0, refCursor = 0, effectCursor = 0, dirty = false, tree;
  const calls = [];
  let config = { ...initialConfig, ...overrides };
  const react = {
    useState(initial) {
      const index = stateCursor++;
      if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [state[index], value => {
        const next = typeof value === 'function' ? value(state[index]) : value;
        if (!Object.is(next, state[index])) { state[index] = next; dirty = true; }
      }];
    },
    useRef(initial) { const index = refCursor++; return refs[index] ??= { current: initial }; },
    useEffect(fn, deps) {
      const index = effectCursor++;
      if (!effects[index] || deps.some((value, i) => !Object.is(value, effects[index].deps[i]))) {
        queue.push(() => { effects[index]?.cleanup?.(); effects[index] = { deps, cleanup: fn() }; });
      }
    },
  };
  const api = {
    fetchAIConfig: async () => config,
    fetchEmbeddingStatus: async () => noIndex,
    discoverAIModels: async payload => { calls.push(['discover', payload]); return { models: [{ id: 'example-model' }] }; },
    updateAIConfig: async payload => {
      calls.push(['chat-save', payload]);
      config = { ...config, provider: payload.provider, timeout_seconds: payload.timeout_seconds,
        ...(payload.openai && { openai_base_url: payload.openai.base_url, openai_model: payload.openai.model }) };
      return config;
    },
    updateEmbeddingConfig: async payload => {
      calls.push(['embedding-save', payload]);
      config = { ...config, embedding_provider: payload.provider, embedding_model: payload.model,
        embedding_base_url: payload.reuse_chat ? config.openai_base_url : payload.base_url,
        has_embedding_api_key: payload.reuse_chat || Boolean(payload.api_key), embedding_timeout_seconds: payload.timeout_seconds };
      return config;
    },
    updateOcrConfig: async payload => {
      calls.push(['ocr-save', payload]);
      config = { ...config, ocr_provider: payload.provider, ocr_model: payload.model,
        ocr_base_url: payload.reuse_chat ? config.openai_base_url : payload.base_url,
        has_ocr_api_key: payload.reuse_chat || Boolean(payload.api_key) };
      return config;
    },
    testAIConfig: async () => { calls.push(['chat-test']); return { ok: true, message: 'ok' }; },
    testOcrConfig: async () => { calls.push(['ocr-test']); return { ok: true, message: 'ok' }; },
    testEmbeddingCompatibility: async () => { calls.push(['embedding-test']); return { ok: true, decision: 'unknown', reason: 'new', profile: { id: 1 }, scores: [] }; },
    ...customApi,
  };
  const Component = load('components/ModelSettingsPage.tsx', {
    react, 'next/link': () => null, 'next/navigation': { useRouter: () => ({ refresh() {} }) },
    '@/components/SettingsNavigationState': { SettingsNavigationRegistration: () => null },
    '@/lib/navigation-guard': { useUnsavedChanges() {}, confirmNavigation: () => false },
    '@/lib/settings-setup': setup, '@/lib/api': api,
  }).default;
  function render() {
    let passes = 0;
    do {
      dirty = false; stateCursor = 0; refCursor = 0; effectCursor = 0;
      tree = Component({ guided: true });
      while (queue.length) queue.shift()();
      if (++passes > 10) throw new Error('effect loop');
    } while (dirty);
  }
  async function settle() { for (let i = 0; i < 5; i++) { await Promise.resolve(); render(); } }
  render(); await settle();
  const find = (predicate) => {
    const found = children(tree).find(predicate);
    assert.ok(found, 'expected rendered control');
    return found;
  };
  const button = (label) => find(node => node.type === 'button' && text(node) === label);
  return {
    calls, find, button, text: () => text(tree),
    field: id => find(node => node.props?.id === id),
    async change(id, value) { find(node => node.props?.id === id).props.onChange(value); await settle(); },
    async click(label) { await button(label).props.onClick(); await settle(); },
    async next() { await button('下一步（也可暂不配置）→').props.onClick(); await settle(); },
    async submit() { await find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await settle(); },
    async action(fn) { await fn(); await settle(); }, settle,
  };
}

test('readiness distinguishes optional, dirty, saved and tested without claiming readiness', () => {
  assert.match(setup.setupChannelState({ enabled: false, dirty: false }).title, /未启用/);
  assert.match(setup.setupChannelState({ enabled: true, dirty: false }).title, /本次未验证/);
  assert.match(setup.setupChannelState({ enabled: true, dirty: false, tested: true }).title, /本次测试通过/);
  assert.match(setup.setupChannelState({ enabled: true, dirty: false, tested: false }).title, /未通过/);
  assert.match(setup.setupChannelState({ enabled: false, dirty: true, tested: true }).title, /未保存/);
  assert.match(setup.setupChannelState({ enabled: true, dirty: false, complete: false }).title, /配置未完整/);
  assert.match(setup.setupChannelState({ enabled: true, dirty: false, paused: true }).title, /外部识别未启用/);
});

test('optional steps do not save, test, disable or build any existing channel', async () => {
  const page = await renderSettings();
  await page.next(); await page.next(); await page.click('查看配置摘要 →');
  assert.match(page.text(), /已配置 · 本次未验证/);
  assert.match(page.text(), /尚无已启用的向量索引/);
  assert.deepEqual(page.calls, []);
});

test('unsaved step offers explicit continue and retains draft without writing', async () => {
  const page = await renderSettings();
  await page.change('openai-model', 'draft-chat');
  await page.next();
  assert.match(page.text(), /此步骤有未保存/);
  await page.click('保留草稿并继续');
  await page.click('上一步');
  assert.equal(page.field('openai-model').props.value, 'draft-chat');
  assert.deepEqual(page.calls, []);
});

test('chat save is independent from a retained embedding draft', async () => {
  const page = await renderSettings();
  await page.next(); await page.click('OpenAI 兼容复用 OpenAI 协议，单独管理密钥');
  await page.change('embedding-model', 'embedding-draft');
  await page.click('上一步'); await page.click('保留草稿并继续');
  await page.change('openai-model', 'chat-next'); await page.submit();
  assert.equal(page.calls[0][0], 'chat-save');
  assert.ok(Object.keys(page.calls[0][1]).every(key => !key.startsWith('embedding')));
  await page.next();
  assert.equal(page.field('embedding-model').props.value, 'embedding-draft');
});

test('embedding reuse discovery and save use saved chat, not its unsaved draft', async () => {
  const page = await renderSettings();
  await page.change('openai-base-url', 'https://unsaved.example/v1');
  await page.next(); await page.click('保留草稿并继续');
  await page.click('OpenAI 兼容复用 OpenAI 协议，单独管理密钥');
  await page.change('embedding-reuse-chat', true);
  await page.change('embedding-model', 'embedding-only');
  await page.action(() => page.field('embedding-model').props.action.onClick());
  assert.equal(page.calls[0][1].channel, 'chat');
  assert.equal(page.calls[0][1].base_url, initialConfig.openai_base_url);
  assert.equal(page.calls[0][1].api_key, undefined);
  await page.submit();
  assert.equal(page.calls[1][0], 'embedding-save');
  assert.equal(page.calls[1][1].reuse_chat, true);
  assert.equal(page.calls[1][1].model, 'embedding-only');
  assert.equal(page.field('embedding-reuse-chat').props.checked, false);
  assert.equal(page.field('embedding-base-url').props.value, initialConfig.openai_base_url);
  assert.equal(page.button('保存向量模型').props.disabled, true);
});

test('OCR reuse uses saved chat and reconciles returned URL after save', async () => {
  const page = await renderSettings();
  await page.change('openai-base-url', 'https://unsaved.example/v1');
  await page.next(); await page.click('保留草稿并继续'); await page.next();
  await page.click('OpenAI 兼容需要支持视觉问答的多模态模型');
  await page.change('ocr-reuse-chat', true); await page.change('ocr-model', 'vision-only');
  await page.action(() => page.field('ocr-model').props.action.onClick());
  assert.equal(page.calls[0][1].base_url, initialConfig.openai_base_url);
  assert.equal(page.calls[0][1].api_key, undefined);
  await page.submit();
  assert.equal(page.calls[1][0], 'ocr-save');
  assert.equal(page.calls[1][1].reuse_chat, true);
  assert.equal(page.field('ocr-reuse-chat').props.checked, false);
  assert.equal(page.field('ocr-base-url').props.value, initialConfig.openai_base_url);
  assert.equal(page.button('保存外部 OCR').props.disabled, true);
});

test('editing a tested value invalidates success even when reverted', async () => {
  const page = await renderSettings();
  await page.click('测试对话连接');
  assert.match(page.text(), /本次测试通过/);
  await page.change('openai-model', 'changed');
  await page.change('openai-model', initialConfig.openai_model);
  assert.doesNotMatch(page.text(), /本次测试通过/);
  assert.match(page.text(), /本次未验证/);
});

test('model discovery from an old connection cannot repopulate new connection choices', async () => {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const page = await renderSettings({}, { discoverAIModels: () => pending });
  page.field('openai-model').props.action.onClick();
  await page.change('openai-base-url', 'https://changed.example/v1');
  resolve({ models: [{ id: 'stale-model' }] }); await page.settle();
  assert.deepEqual(page.field('openai-model').props.models, []);
  assert.equal(page.field('openai-model').props.action.busy, false);
  assert.equal(page.field('openai-model').props.action.disabled, true);
});

test('unconfigured or Ollama chat cannot be copied as OpenAI credentials', async () => {
  const page = await renderSettings({ provider: 'ollama', has_api_key: true });
  await page.next(); await page.click('OpenAI 兼容复用 OpenAI 协议，单独管理密钥');
  assert.equal(page.field('embedding-reuse-chat').props.available, false);
});

test('saved providers missing model or key are not labelled configured', async () => {
  for (const missing of [{ openai_model: '' }, { has_api_key: false }, { openai_base_url: null }]) {
    const page = await renderSettings(missing);
    assert.match(page.text(), /配置未完整/);
    assert.doesNotMatch(page.text(), /已配置 · 本次未验证/);
  }
});

test('OCR with zero external pages is distinct from disabled provider', async () => {
  const page = await renderSettings({ ocr_provider: 'openai', ocr_model: 'vision', ocr_base_url: 'https://ocr.example/v1', has_ocr_api_key: true, ocr_max_external_pages: 0 });
  assert.match(page.text(), /已配置 · 外部识别未启用/);
});

test('switching reused embedding to disabled does not send a hidden copy flag', async () => {
  const page = await renderSettings();
  await page.next(); await page.click('OpenAI 兼容复用 OpenAI 协议，单独管理密钥');
  await page.change('embedding-reuse-chat', true);
  await page.change('embedding-model', 'embedding-draft');
  await page.click('不使用关闭 Embedding，仅使用关键词检索');
  await page.submit();
  assert.equal(page.calls[0][0], 'embedding-save');
  assert.equal(page.calls[0][1].provider, 'disabled');
  assert.equal(page.calls[0][1].reuse_chat, undefined);
});

test('switching reused OCR to disabled does not send a hidden copy flag', async () => {
  const page = await renderSettings();
  await page.next(); await page.next();
  await page.click('OpenAI 兼容需要支持视觉问答的多模态模型');
  await page.change('ocr-reuse-chat', true);
  await page.change('ocr-model', 'vision-draft');
  await page.click('不使用只使用本地 tesseract，不再外发图片');
  await page.submit();
  assert.equal(page.calls[0][0], 'ocr-save');
  assert.equal(page.calls[0][1].provider, 'disabled');
  assert.equal(page.calls[0][1].reuse_chat, undefined);
});

test('chat save invalidates models discovered through a selected reuse connection', async () => {
  const page = await renderSettings();
  await page.next(); await page.click('OpenAI 兼容复用 OpenAI 协议，单独管理密钥');
  await page.change('embedding-reuse-chat', true);
  await page.action(() => page.field('embedding-model').props.action.onClick());
  assert.equal(page.field('embedding-model').props.models.length, 1);
  await page.click('上一步'); await page.click('保留草稿并继续');
  await page.change('openai-model', 'chat-revised'); await page.submit();
  await page.next();
  assert.deepEqual(page.field('embedding-model').props.models, []);
  assert.equal(page.field('embedding-reuse-chat').props.checked, true);
});

test('failed save preserves a credential-bearing draft and reports error', async () => {
  const page = await renderSettings({}, { updateAIConfig: async () => { throw new Error('synthetic save failed'); } });
  await page.change('openai-model', 'not-saved'); await page.submit();
  assert.equal(page.field('openai-model').props.value, 'not-saved');
  assert.match(page.text(), /synthetic save failed/);
  assert.equal(page.button('保存对话模型').props.disabled, false);
});

test('a selected copy can be cancelled after its saved chat source is disabled', async () => {
  const page = await renderSettings();
  await page.next(); await page.click('OpenAI 兼容复用 OpenAI 协议，单独管理密钥');
  await page.change('embedding-reuse-chat', true);
  await page.click('上一步'); await page.click('保留草稿并继续');
  await page.click('不使用资料会正常入库，但不会调用 AI'); await page.submit();
  await page.next();
  const control = page.field('embedding-reuse-chat');
  assert.equal(control.props.available, false);
  assert.equal(control.props.checked, true);
  const rendered = control.type(control.props);
  assert.equal(children(rendered).find(node => node.type === 'input').props.disabled, false);
  await page.change('embedding-reuse-chat', false);
  assert.equal(page.field('embedding-base-url').props.disabled, false);
});

test('disabled saved providers do not expose old saved keys to model discovery', async () => {
  const page = await renderSettings({ provider: 'disabled', has_api_key: true });
  await page.click('OpenAI 兼容支持 OpenAI 以及兼容协议的服务');
  assert.equal(page.field('openai-model').props.action.disabled, true);
  await page.next(); await page.click('保留草稿并继续');
  await page.click('OpenAI 兼容复用 OpenAI 协议，单独管理密钥');
  assert.equal(page.field('embedding-model').props.action.disabled, true);
});
