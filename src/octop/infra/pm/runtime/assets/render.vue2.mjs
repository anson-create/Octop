/**
 * SSR sandbox for a Vue 2 + Element UI page — the sibling of `render.mjs`.
 *
 * Why a second script instead of a branch in the first one: CargoWare is Vue
 * 2.6 on webpack 4. It has no `createSSRApp`, no `@vue/server-renderer`, no
 * Vite, and components that Vue 3's renderer resolves by name are here resolved
 * from a `components: {}` option or registered globally in `main.js`. Sharing a
 * script would mean every line carrying a "which generation are we in" branch,
 * and the Vue 3 path — which is measured and working — would pay the risk.
 *
 * What it does:
 *   1. boots Vite (from the toolchain) over the *mirror*, so the page's own
 *      `.vue`, `@/` alias and `node_modules` are the product's real ones;
 *   2. loads the page module through that Vite instance, so `vue` is a single
 *      instance shared with the page;
 *   3. registers Element UI, the repo's own `vue-i18n` catalogue and, when it
 *      loads, the repo's store — the same three globals `main.js` installs;
 *   4. renders with `vue-server-renderer` and prints JSON.
 *
 * Deliberate limits, each of which the caller must be able to see:
 *   - `mounted()` does not run under SSR, so the page renders in its empty
 *     state. Filling in rows is the caller's job: substitute text, never
 *     restructure.
 *   - network is refused (see `blockNetwork`). A page that fetches in
 *     `created()` therefore renders empty instead of hanging for the timeout.
 *   - styles are *not* collected here. Vue 2's renderer has no module graph to
 *     ask; the Python side inlines `dist/pm-prototype.css` or the Element UI
 *     bundle instead.
 *
 * Dependency resolution: `vite`, `vite-plugin-vue2` and `vue-server-renderer`
 * are resolved from this script's directory, so the script is copied into the
 * toolchain directory rather than into the mirror. `vue`, `element-ui` and the
 * rest resolve through Vite against the mirror — they must be the product's
 * versions, not the toolchain's.
 *
 * Usage:
 *   node render.vue2.mjs --root <mirror> --page </src/views/x/index.vue> [--out file]
 *   node render.vue2.mjs --root <mirror> --selftest
 * Prints one JSON object: { html, styles[], components[], errors[], error? }
 */

import { writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

function arg(name, fallback = '') {
  const i = process.argv.indexOf(`--${name}`)
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback
}

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(arg('root', '.'))
const page = arg('page', '')
const out = arg('out', '')
const selftest = process.argv.includes('--selftest')

const result = { html: '', styles: [], components: [], errors: [] }

/**
 * Resolve one of the renderer's own packages.
 *
 * Plain `import` first — correct whenever this script sits next to the
 * toolchain's `node_modules`, which is where it is deployed. The fallback goes
 * through `require.resolve` so a script run from elsewhere still finds the
 * package's real entry point rather than a guessed `index.js`.
 */
async function loadOwn(name) {
  try {
    return await import(name)
  } catch (first) {
    try {
      const entry = createRequire(join(here, 'noop.js')).resolve(name)
      return await import(pathToFileURL(entry).href)
    } catch {
      throw new Error(`cannot resolve ${name} from ${here}: ${first?.message || first}`)
    }
  }
}

/**
 * Define a global that modern Node may already own as a read-only accessor.
 *
 * Node 22 exposes `navigator` as a getter-only property; a plain assignment
 * throws "Cannot set property navigator of #<Object> which has only a getter"
 * before a single component renders. `defineProperty` is the only way in.
 */
function defineGlobal(name, value) {
  try {
    Object.defineProperty(globalThis, name, {
      value,
      configurable: true,
      writable: true,
      enumerable: false,
    })
  } catch {
    globalThis[name] = value
  }
}

/**
 * The page is a browser app: it may touch `document.cookie` or `navigator` at
 * module scope. `window` is deliberately *not* defined — Vue 2 decides
 * "am I on a server" from `typeof window`, and a fake one would push it into
 * client-side code paths that need a real DOM.
 */
function installBrowserShims() {
  const noop = () => {}
  const memoryStorage = () => {
    const store = new Map()
    return {
      getItem: (k) => (store.has(String(k)) ? store.get(String(k)) : null),
      setItem: (k, v) => store.set(String(k), String(v)),
      removeItem: (k) => store.delete(String(k)),
      clear: () => store.clear(),
      key: (i) => [...store.keys()][i] ?? null,
      get length() {
        return store.size
      },
    }
  }
  defineGlobal('localStorage', memoryStorage())
  defineGlobal('sessionStorage', memoryStorage())
  defineGlobal('navigator', {
    userAgent: 'node',
    language: 'zh-CN',
    languages: ['zh-CN', 'zh'],
  })

  const element = () => ({
    style: {},
    dataset: {},
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    setAttribute: noop,
    removeAttribute: noop,
    appendChild: (child) => child,
    removeChild: (child) => child,
    insertBefore: (child) => child,
    addEventListener: noop,
    removeEventListener: noop,
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }),
    querySelector: () => null,
    querySelectorAll: () => [],
    cloneNode: () => element(),
  })

  defineGlobal('document', {
    cookie: '',
    title: '',
    documentElement: element(),
    head: element(),
    body: element(),
    createElement: () => element(),
    createTextNode: (text) => ({ textContent: text }),
    createComment: (text) => ({ textContent: text }),
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementById: () => null,
    addEventListener: noop,
    removeEventListener: noop,
  })
}

/**
 * Refuse the network instead of waiting on it.
 *
 * CargoWare's `created()` hooks call `src/utils/request.js`, which is an axios
 * instance. axios picks its adapter from `typeof XMLHttpRequest`: without one
 * it uses Node's http client and the render sits on a real socket until the
 * caller's timeout. Defining a stub that fails on `send()` turns that into an
 * immediate rejection the page's own `catch` already handles.
 */
function blockNetwork() {
  class BlockedXhr {
    constructor() {
      this.readyState = 0
      this.status = 0
      this.timeout = 0
      this.responseText = ''
      this.response = ''
      this.onreadystatechange = null
      this.onerror = null
      this.ontimeout = null
      this.onabort = null
    }
    open() {}
    setRequestHeader() {}
    getAllResponseHeaders() {
      return ''
    }
    abort() {}
    send() {
      result.errors.push('xhr blocked')
      setTimeout(() => {
        this.readyState = 4
        this.status = 0
        this.onerror?.(new Error('network blocked during SSR'))
        this.onreadystatechange?.()
      }, 0)
    }
  }
  defineGlobal('XMLHttpRequest', BlockedXhr)

  const originalFetch = globalThis.fetch
  defineGlobal('fetch', async (...args) => {
    result.errors.push(`fetch blocked: ${String(args[0]).slice(0, 120)}`)
    return new Response(JSON.stringify({ code: 0, data: { list: [], total: 0 } }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  })
  return () => {
    globalThis.fetch = originalFetch
  }
}

/** Try a list of module specifiers through Vite, keeping the first that loads. */
async function firstModule(vite, candidates) {
  for (const specifier of candidates) {
    try {
      return { specifier, mod: await vite.ssrLoadModule(specifier) }
    } catch {
      // next candidate
    }
  }
  return null
}

async function main() {
  process.env.VUE_ENV = 'server'
  process.env.NODE_ENV = process.env.NODE_ENV || 'production'
  installBrowserShims()

  const viteMod = await loadOwn('vite')
  const createServer = viteMod.createServer ?? viteMod.default?.createServer
  if (typeof createServer !== 'function') throw new Error('vite createServer unavailable')

  // Vue 2's SFC plugin has shipped under two names; the toolchain installs one
  // of them and this reports which, rather than hard-coding a guess.
  let vue2plugin = null
  let pluginName = ''
  for (const name of ['vite-plugin-vue2', '@vitejs/plugin-vue2']) {
    try {
      const mod = await loadOwn(name)
      vue2plugin = mod.default ?? mod
      pluginName = name
      break
    } catch {
      // try the next plugin name
    }
  }
  if (vue2plugin === null) throw new Error('no Vue 2 SFC plugin in the toolchain')
  result.components.push(`plugin:${pluginName}`)

  const vueServerMod = await loadOwn('vue-server-renderer')
  const createRenderer = vueServerMod.createRenderer ?? vueServerMod.default?.createRenderer
  if (typeof createRenderer !== 'function') throw new Error('vue-server-renderer unavailable')

  if (selftest) {
    // Proves the toolchain resolves; says nothing about the mirror, which is
    // the caller's separate check. Kept as a mode so a deployment can be
    // verified in one command instead of by attempting a render.
    result.components.push(`root:${root}`, `toolchain:${here}`, `node:${process.version}`)
    return
  }

  if (!page) throw new Error('missing --page')

  const vite = await createServer({
    root,
    // The app's aliases live in vue.config.js (webpack); Vite never reads it.
    resolve: { alias: { '@': join(root, 'src'), '~': join(root, 'src') } },
    plugins: [vue2plugin()],
    server: { middlewareMode: true },
    appType: 'custom',
    logLevel: 'error',
    // webpack projects read configuration from `process.env` at module scope.
    // Vite leaves `process` undefined in the browser-like graph, so every such
    // read would throw before the page renders.
    define: { 'process.env': '({})' },
  })

  try {
    // Vue must come *through Vite* so the page and this script share one
    // instance; a second copy from the toolchain would fail `Vue.use` and every
    // component check with "different Vue" warnings.
    const vueMod = await vite.ssrLoadModule('vue')
    const Vue = vueMod.default ?? vueMod
    result.components.push(`vue:${Vue.version}`)

    const pageMod = await vite.ssrLoadModule(page.startsWith('/') ? page : `/${page}`)
    const Page = pageMod.default
    if (!Page) throw new Error(`no default export in ${page}`)

    // Unknown components are the single most common cause of a page that
    // renders "almost right" — CargoWare registers overrides such as
    // cargo-el-input globally in main.js, which this sandbox does not run.
    // Collecting the warnings makes that visible instead of silent.
    Vue.config.warnHandler = (msg) => {
      if (result.errors.length < 40) result.errors.push(String(msg).slice(0, 200))
    }

    // The version pairing is not optional: the template compiler emits render
    // functions for its own Vue build, and a mismatch makes every component
    // render as an empty comment.
    try {
      const compiler = await vite.ssrLoadModule('vue-template-compiler')
      const version = (compiler.default ?? compiler).version
      if (version && version !== Vue.version) {
        result.errors.push(`vue-template-compiler ${version} != vue ${Vue.version}`)
      }
    } catch (err) {
      result.errors.push(`vue-template-compiler: ${err.message}`)
    }

    // --- Element UI, globally, exactly as main.js does ---------------------
    try {
      const element = await vite.ssrLoadModule('element-ui')
      Vue.use(element.default ?? element)
      result.components.push('element-ui:global')
    } catch (err) {
      result.errors.push(`element-ui: ${err.message}`)
    }

    // --- the repo's own catalogue, so copy matches the product ------------
    const lang = await firstModule(vite, [
      '/src/lang/index.js',
      '/src/lang/index.ts',
      '/src/i18n/index.js',
      '/src/i18n/index.ts',
    ])
    if (lang) {
      const i18n = lang.mod.i18n ?? lang.mod.default
      if (i18n && typeof i18n.install === 'function') {
        Vue.use(i18n)
        // With no browser there is no `navigator.language` the app can trust,
        // so pin the locale the product ships. vue-i18n 7 exposes it as a plain
        // string; 8+ wraps it in a ref.
        try {
          if (i18n.locale && typeof i18n.locale === 'object' && 'value' in i18n.locale) {
            i18n.locale.value = 'zh'
          } else {
            i18n.locale = 'zh'
          }
        } catch (err) {
          result.errors.push(`locale: ${err.message}`)
        }
        result.components.push(`i18n:${lang.specifier}`)
      }
    } else {
      result.errors.push('i18n: no catalogue found (src/lang, src/i18n)')
    }

    // --- vue-router 3, abstract mode: one page, no navigation -------------
    let router = null
    try {
      const routerMod = await vite.ssrLoadModule('vue-router')
      const Router = routerMod.default ?? routerMod
      Vue.use(Router)
      router = new Router({
        mode: 'abstract',
        routes: [{ path: '/', component: { name: 'SsrPlaceholder', render: (h) => h('div') } }],
      })
      result.components.push('router:abstract')
    } catch (err) {
      result.errors.push(`router: ${err.message}`)
    }

    // --- the repo's store, when it has one --------------------------------
    let store = null
    const storeLoaded = await firstModule(vite, ['/src/store/index.js', '/src/store/index.ts'])
    if (storeLoaded) {
      const candidate = storeLoaded.mod.default ?? storeLoaded.mod.store
      if (candidate && typeof candidate.commit === 'function') {
        store = candidate
        result.components.push(`store:${storeLoaded.specifier}`)
      }
    }

    const restore = blockNetwork()
    try {
      const app = new Vue({
        render: (h) => h(Page),
        ...(router ? { router } : {}),
        ...(store ? { store } : {}),
      })
      result.html = await createRenderer().renderToString(app)
    } finally {
      restore()
    }
  } finally {
    await vite.close()
  }
}

main()
  .then(() => {
    const payload = JSON.stringify(result)
    if (out) writeFileSync(out, payload)
    else process.stdout.write(payload)
    // Vite keeps an optimizer and watcher alive, so the event loop never
    // drains on its own.
    process.exit(0)
  })
  .catch((err) => {
    console.log(JSON.stringify({ ...result, error: err?.message || String(err) }))
    process.exit(1)
  })
