/**
 * Host half of the MCP status plugin.
 *
 * Не изобретаем проверку заново: переиспользуем отлаженный скрипт скилла
 * `dsh-mcp-status`. Он делает настоящий MCP-хендшейк по каждому серверу из
 * конфига профиля и пишет JSON в файл (через пайп нельзя — вывод забирает
 * песочница). Здесь этот JSON кэшируется и раздаётся по HTTP, чтобы кнопка в
 * интерфейсе открывалась мгновенно.
 */

import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, appendFileSync } from 'node:fs'
import { readFile, rm } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

const ROUTE = '/plugin/mcp-status'
const REFRESH_TIMEOUT_MS = 180000
const DEFAULT_PYTHON = join(
  homedir(),
  '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python',
  process.platform === 'win32' ? 'python.exe' : 'bin/python3',
)
// скрипт едет вместе с плагином: пакет самодостаточен, ничего ставить рядом не надо
const DEFAULT_SCRIPT = join(HERE, 'scripts', 'mcp_status.py')

// маркер загрузки модуля: отличает «пакет не импортирован» от «apply упал»
try {
  appendFileSync(join(HERE, '.activation.log'), `${new Date().toISOString()} import\n`)
} catch {
  // диагностика не должна мешать загрузке
}

// плагин живёт в веб-профиле: без веб-сервера ему некуда регистрировать маршрут
export const inject = ['webServer']

export function apply(ctx, config = {}) {
  const trace = (note) => {
    try {
      appendFileSync(join(HERE, '.activation.log'), `${new Date().toISOString()} ${note}\n`)
    } catch {
      // диагностика не должна мешать загрузке
    }
  }

  const pythonPath = config.pythonPath || DEFAULT_PYTHON
  const scriptPath = config.scriptPath || DEFAULT_SCRIPT
  const intervalMs = Math.max(15000, Number(config.intervalMs) || 300000)

  const state = {
    data: null,
    fetchedAt: 0,
    durationMs: null,
    running: false,
    error: existsSync(scriptPath) ? null : `скрипт не найден: ${scriptPath}`,
  }

  const log = (message) => {
    try {
      ctx.logger?.warn?.(`[mcp-status] ${message}`)
    } catch {
      // логгер есть не во всех композициях
    }
  }

  const payload = () => ({
    ok: state.data !== null,
    fetchedAt: state.fetchedAt,
    durationMs: state.durationMs,
    refreshing: state.running,
    error: state.error,
    data: state.data,
  })

  function refresh() {
    if (state.running) return Promise.resolve(payload())
    if (!existsSync(scriptPath)) {
      state.error = `скрипт не найден: ${scriptPath}`
      return Promise.resolve(payload())
    }

    state.running = true
    const started = Date.now()
    const outFile = join(tmpdir(), `dsh-mcp-status-${process.pid}-${randomUUID()}.json`)

    return new Promise((resolve) => {
      let settled = false

      const finish = async (failure) => {
        if (settled) return
        settled = true
        state.running = false
        state.durationMs = Date.now() - started

        let text = null
        try {
          text = await readFile(outFile, 'utf8')
        } catch {
          // файла может не быть, если процесс упал до записи
        }
        try {
          await rm(outFile, { force: true })
        } catch {
          // временный файл мог не создаться
        }

        if (!text) {
          state.error = failure || 'скрипт не оставил результата'
          log(state.error)
          resolve(payload())
          return
        }
        try {
          state.data = JSON.parse(text)
          state.fetchedAt = Date.now()
          state.error = null
        } catch (parseError) {
          state.error = `не удалось разобрать вывод: ${String(parseError)}`
          log(state.error)
        }
        resolve(payload())
      }

      let child
      try {
        child = spawn(pythonPath, [scriptPath, '--json', '--out', outFile], {
          stdio: 'ignore',
          windowsHide: true,
        })
      } catch (error) {
        void finish(String(error))
        return
      }

      const timer = setTimeout(() => {
        try {
          child.kill()
        } catch {
          // процесс мог уже завершиться
        }
        void finish(`таймаут проверки (${REFRESH_TIMEOUT_MS / 1000} с)`)
      }, REFRESH_TIMEOUT_MS)
      timer.unref?.()

      child.on('error', (error) => {
        clearTimeout(timer)
        void finish(String(error))
      })
      // код возврата 1 — нормальный случай: скрипт сообщает о мёртвых серверах
      child.on('exit', (code, signal) => {
        clearTimeout(timer)
        void finish(`скрипт завершился с кодом ${code ?? signal}`)
      })
    })
  }

  const registerRoute = (server) => {
    if (!server || typeof server.register !== 'function') {
      log('сервис webServer без метода register')
      return () => {}
    }
    try {
      return server.register({
        kind: 'exact',
        path: ROUTE,
        handler: (request, response) => {
          const url = new URL(request.url ?? ROUTE, 'http://localhost')
          if (url.searchParams.get('refresh') === '1' || state.data === null) void refresh()
          const body = JSON.stringify(payload())
          response.writeHead(200, {
            'content-type': 'application/json; charset=utf-8',
            'cache-control': 'no-store',
          })
          response.end(body)
        },
      })
    } catch (error) {
      log(`маршрут ${ROUTE} не зарегистрирован: ${String(error)}`)
      return () => {}
    }
  }

  try {
  ctx.effect(() => {
    const disposeRoute = registerRoute(ctx.webServer)

    void refresh()
    const timer = setInterval(() => void refresh(), intervalMs)
    timer.unref?.()

    return () => {
      disposeRoute()
      clearInterval(timer)
    }
  })
  } catch (error) {
    trace(`effect-error: ${String((error && error.stack) || error).split('\n').slice(0, 3).join(' | ')}`)
    throw error
  }
}
