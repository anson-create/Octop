/**
 * SSR sandbox: render one real page component to static HTML.
 *
 * Why this exists: the prototype step used to ask a model to "draw" a page from
 * a sentence, which produced a generic admin shell with invented class names.
 * For a prototype to be 1:1 with the product it has to be *produced by the
 * product's own components*. This script boots the repo's own Vite config,
 * imports the real .vue page, renders it with Vue's SSR renderer, and prints the
 * resulting markup together with the stylesheets the page needs.
 *
 * This file is the single source of truth. `prototype_factory.ensure_render_script`
 * copies it into the repository mirror at run time, because bare specifiers
 * (`vite`, `vue`, `@arco-design/web-vue`) resolve relative to the *script*, not
 * the working directory — running it from site-packages would not find the
 * mirror's node_modules. Do not edit the copies inside /opt/pm-mirror; they are
 * overwritten whenever this file's hash changes.
 *
 * Scope: Vue 3 + Arco only. CargoWare (Vue 2 + Element UI) needs
 * `vue-server-renderer` and a different component registration strategy, so it
 * gets its own sibling script rather than a pile of branches in this one.
 *
 * Two things are stubbed, and only these two:
 *   - the router, because a single page has no navigation;
 *   - network calls, because SSR does not run `onMounted`, so a page renders in
 *     its empty state. Filling in rows is the caller's job: it must keep this
 *     markup and only substitute text, never restructure.
 *
 * Arco is registered globally on purpose. The app relies on
 * `unplugin-vue-components` auto-import, which does not run for SSR transforms;
 * without an explicit `app.use(ArcoVue)` every `<a-card>` would render as an
 * unknown, empty tag and the output would look nothing like the product.
 *
 * Usage:
 *   node render.mjs --root <repo-root> --page </src/views/x/index.vue> [--out file]
 * Prints a JSON object: { html, styles[], components[], errors[], error? }
 */

import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

function arg(name, fallback = '') {
  const i = process.argv.indexOf(`--${name}`)
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback
}

const root = resolve(arg('root', '.'))
const page = arg('page', '')
const out = arg('out', '')

if (!page) {
  console.log(JSON.stringify({ error: 'missing --page' }))
  process.exit(2)
}

const result = { html: '', styles: [], components: [], errors: [] }

async function main() {
  // The app reads localStorage while its i18n module is being evaluated (to
  // pick a default locale). There is no browser here, so provide an in-memory
  // equivalent rather than letting the whole render fail on a missing global.
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
  globalThis.localStorage = memoryStorage()
  globalThis.sessionStorage = memoryStorage()

  // Import through the package specifier so Node picks the ESM entry from the
  // package's exports map. Resolving to a file path first lands on the CJS
  // build, where `createServer` is not a named export.
  const viteMod = await import('vite')
  const vueMod = await import('vue')
  const ssrMod = await import('@vue/server-renderer')
  const createServer = viteMod.createServer ?? viteMod.default?.createServer
  const createSSRApp = vueMod.createSSRApp ?? vueMod.default?.createSSRApp
  const renderToString = ssrMod.renderToString ?? ssrMod.default?.renderToString
  if (typeof createServer !== 'function') throw new Error('vite createServer unavailable')
  if (typeof createSSRApp !== 'function') throw new Error('vue createSSRApp unavailable')
  if (typeof renderToString !== 'function') throw new Error('ssr renderToString unavailable')

  const vite = await createServer({
    root,
    // middlewareMode keeps Vite out of the network entirely: no port, no server.
    server: { middlewareMode: true },
    appType: 'custom',
    logLevel: 'error',
  })

  // The page's own CSS imports are collected below from the module graph.
  // Without them the markup is structurally right but visually wrong, which is
  // exactly the failure mode we are trying to eliminate.
  const pageMod = await vite.ssrLoadModule(page)
  const component = pageMod.default
  if (!component) throw new Error(`no default export in ${page}`)

  const app = createSSRApp(component)

  // --- Arco (global registration, see the header note) --------------------
  try {
    const arco = await vite.ssrLoadModule('@arco-design/web-vue')
    app.use(arco.default ?? arco)
    result.components.push('arco:global')
  } catch (err) {
    result.errors.push(`arco: ${err.message}`)
  }

  // --- router stub --------------------------------------------------------
  try {
    const routerMod = await vite.ssrLoadModule('vue-router')
    const router = routerMod.createRouter({
      history: routerMod.createMemoryHistory(),
      routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()
    app.use(router)
    result.components.push('router:memory')
  } catch (err) {
    result.errors.push(`router: ${err.message}`)
  }

  // --- i18n: use the repo's real catalogue so copy matches the product ----
  for (const candidate of ['/src/i18n/index.ts', '/src/i18n/index.js', '/src/i18n/index.tsx']) {
    try {
      const mod = await vite.ssrLoadModule(candidate)
      const instance = mod.i18n ?? mod.default
      if (instance && typeof instance.install === 'function') {
        app.use(instance)
        // With no browser there is no navigator.language, so the app's i18n
        // module falls back to its default and the prototype's copy comes out in
        // English. The product ships zh-CN; pin it before rendering.
        try {
          const global = instance.global ?? instance
          const current = global.locale
          if (current && typeof current === 'object' && 'value' in current) current.value = 'zh-CN'
          else if (typeof current === 'string') global.locale = 'zh-CN'
        } catch (err) {
          result.errors.push(`locale: ${err.message}`)
        }
        result.components.push(`i18n:${candidate}`)
      }
      break
    } catch {
      // try the next candidate path
    }
  }

  // --- pinia --------------------------------------------------------------
  try {
    const piniaMod = await vite.ssrLoadModule('pinia')
    app.use(piniaMod.createPinia())
    result.components.push('pinia')
  } catch {
    // the page may not use a store; absence is not an error
  }

  // --- network: fail loudly in the log, quietly for the render ------------
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (...args) => {
    result.errors.push(`fetch blocked: ${String(args[0]).slice(0, 120)}`)
    return new Response(JSON.stringify({ code: 0, data: { list: [], total: 0 } }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }

  try {
    result.html = await renderToString(app)
  } finally {
    globalThis.fetch = originalFetch
  }

  // Collect the CSS the page actually pulled in (Vite exposes it on the module
  // graph); this is what makes the output look like the product rather than
  // like unstyled markup.
  try {
    for (const [id, mod] of vite.moduleGraph.idToModuleMap) {
      if (mod.type === 'css') result.styles.push(id)
    }
  } catch (err) {
    result.errors.push(`styles: ${err.message}`)
  }

  await vite.close()
}

main()
  .then(() => {
    const payload = JSON.stringify(result)
    if (out) writeFileSync(out, payload)
    else process.stdout.write(payload)
    // Vite keeps a watcher and an optimizer alive, so the event loop never
    // drains. Without this the process hangs until the caller's timeout even
    // though the render already succeeded.
    process.exit(0)
  })
  .catch((err) => {
    console.log(JSON.stringify({ ...result, error: err?.message || String(err) }))
    process.exit(1)
  })
