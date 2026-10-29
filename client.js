/**
 * Client half of the MCP status plugin.
 *
 * Кнопка «MCP» в шапке чата: точка состояния + поповер с таблицей серверов.
 * Данные берёт с хоста по HTTP (маршрут /plugin/mcp-status), поэтому открытие
 * мгновенное — проверка уже сделана в фоне.
 */

window.__ModuleLoader__.load({
  id: '@local/dsh-mcp-status',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useCallback, useEffect, useRef, useState } = React

    const ENDPOINT = '/plugin/mcp-status'
    const POLL_MS = 20000

    const COLORS = {
      ok: '#3fb950',
      warn: '#d29922',
      dead: '#f85149',
      off: 'rgba(230,230,230,0.35)',
      skipped: 'rgba(230,230,230,0.35)',
    }
    const LABELS = {
      ok: 'OK',
      warn: 'ЧАСТИЧНО',
      dead: 'МЁРТВ',
      off: 'ВЫКЛ',
      skipped: 'НЕ ПРОВЕРЕН',
    }
    const STATE_WORD = {
      ok: 'OK',
      warn: 'ЧАСТИЧНО',
      dead: 'МЁРТВ',
      off: 'ВЫКЛ',
      skipped: 'НЕ ПРОВЕРЕН',
    }

    const T = {
      text: 'var(--dsw-alias-text-primary, #e6e6e6)',
      dim: 'var(--dsw-alias-text-secondary, rgba(230,230,230,0.6))',
      faint: 'var(--dsw-alias-text-tertiary, rgba(230,230,230,0.4))',
      bg: 'var(--dsw-alias-bg-elevated, #1c1c20)',
      border: 'var(--dsw-alias-border-subtle, rgba(255,255,255,0.12))',
      hover: 'var(--dsw-alias-bg-hover, rgba(255,255,255,0.06))',
    }

    function readStatus(payload) {
      const servers = payload?.data?.servers ?? []
      const edt = payload?.data?.edt ?? []
      const worst = servers.some((item) => item.state === 'dead')
        ? 'dead'
        : servers.some((item) => item.state === 'warn')
          ? 'warn'
          : payload?.ok
            ? 'ok'
            : 'skipped'
      return { servers, edt, worst, error: payload?.error ?? null, fetchedAt: payload?.fetchedAt ?? 0 }
    }

    function timeOf(stamp) {
      if (!stamp) return '—'
      const date = new Date(stamp)
      return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`
    }

    function Dot({ state, size = 7 }) {
      return h('span', {
        style: {
          width: size,
          height: size,
          borderRadius: '50%',
          background: COLORS[state] ?? COLORS.skipped,
          display: 'inline-block',
          flex: '0 0 auto',
        },
      })
    }

    function Row({ server }) {
      const state = server.state ?? 'skipped'
      return h(
        'div',
        {
          style: {
            display: 'grid',
            gridTemplateColumns: '10px 1fr auto auto',
            gap: '8px',
            alignItems: 'center',
            padding: '5px 10px',
            fontSize: '12px',
            fontFamily: 'var(--dsw-alias-font-mono, ui-monospace, Consolas, monospace)',
          },
        },
        h(Dot, { state }),
        h(
          'div',
          { style: { minWidth: 0 } },
          h('div', { style: { color: T.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, server.name),
          h(
            'div',
            { style: { color: T.faint, fontSize: '11px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
            server.endpoint,
          ),
        ),
        h('div', { style: { color: COLORS[state] ?? T.dim, fontSize: '11px', whiteSpace: 'nowrap' } }, LABELS[state] ?? state),
        h(
          'div',
          { style: { color: T.dim, fontSize: '11px', textAlign: 'right', minWidth: '44px', whiteSpace: 'nowrap' } },
          server.tools === null || server.tools === undefined ? '—' : `${server.tools} инстр.`,
        ),
      )
    }

    function SectionTitle({ children }) {
      return h(
        'div',
        {
          style: {
            padding: '8px 10px 4px',
            fontSize: '10px',
            letterSpacing: '0.08em',
            textTransform: 'uppercase',
            color: T.faint,
          },
        },
        children,
      )
    }

    function Panel({ status, onRefresh, refreshing }) {
      const { servers, edt, error } = status
      const alive = servers.filter((item) => item.state === 'ok').length
      const edtAlive = edt.filter((item) => item.state === 'ok').length

      return h(
        'div',
        {
          role: 'dialog',
          'aria-label': 'MCP-серверы',
          onMouseDown: (event) => event.stopPropagation(),
          style: {
            position: 'absolute',
            top: 'calc(100% + 6px)',
            right: 0,
            width: '440px',
            maxHeight: '60vh',
            overflow: 'auto',
            background: T.bg,
            color: T.text,
            border: `1px solid ${T.border}`,
            borderRadius: '10px',
            boxShadow: '0 12px 32px rgba(0,0,0,0.45)',
            zIndex: 40,
            paddingBottom: '6px',
          },
        },
        h(
          'div',
          {
            style: {
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              justifyContent: 'space-between',
              padding: '8px 10px 6px',
              borderBottom: `1px solid ${T.border}`,
              position: 'sticky',
              top: 0,
              background: T.bg,
            },
          },
          h('div', { style: { fontSize: '12px', fontWeight: 600 } }, 'MCP-серверы'),
          h(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
            h('span', { style: { fontSize: '11px', color: T.dim } }, timeOf(status.fetchedAt)),
            h(
              'button',
              {
                type: 'button',
                onClick: onRefresh,
                disabled: refreshing,
                style: {
                  fontSize: '11px',
                  color: T.text,
                  background: 'transparent',
                  border: `1px solid ${T.border}`,
                  borderRadius: '6px',
                  padding: '2px 8px',
                  cursor: refreshing ? 'default' : 'pointer',
                  opacity: refreshing ? 0.6 : 1,
                },
              },
              refreshing ? 'Проверяю…' : 'Обновить',
            ),
          ),
        ),

        error
          ? h('div', { style: { padding: '10px', fontSize: '11px', color: COLORS.dead } }, error)
          : null,

        servers.length === 0
          ? h('div', { style: { padding: '12px 10px', fontSize: '12px', color: T.dim } }, 'нет данных о серверах')
          : h(
              'div',
              { style: { paddingTop: '2px' } },
              h(SectionTitle, null, `Подключено к DSH — ${alive} из ${servers.length}`),
              servers.map((server) => h(Row, { key: server.name, server })),
            ),

        edt.length > 0
          ? h(
              'div',
              { style: { marginTop: '4px', borderTop: `1px solid ${T.border}` } },
              h(SectionTitle, null, `Инстансы EDT — ${edtAlive} из ${edt.length} на связи`),
              edt.map((row) =>
                h(Row, {
                  key: row.project,
                  server: {
                    name: row.project,
                    endpoint: row.url ?? '—',
                    state: row.state ?? 'skipped',
                    tools: row.tools,
                  },
                }),
              ),
            )
          : null,
      )
    }

    function McpStatusButton() {
      const [open, setOpen] = useState(false)
      const [payload, setPayload] = useState(null)
      const [busy, setBusy] = useState(false)
      const [failed, setFailed] = useState(null)
      const rootRef = useRef(null)

      const load = useCallback(async (force) => {
        try {
          if (force) setBusy(true)
          const response = await fetch(force ? `${ENDPOINT}?refresh=1` : ENDPOINT, { cache: 'no-store' })
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          const body = await response.json()
          setPayload(body)
          setFailed(null)
        } catch (error) {
          setFailed(String(error?.message ?? error))
        } finally {
          if (force) setBusy(false)
        }
      }, [])

      // хост проверяет серверы в фоне (~15 с), поэтому «Проверяю…» держится по его
      // собственному флагу, а не по факту ответа на запрос
      const refreshing = busy || Boolean(payload?.refreshing)

      useEffect(() => {
        void load(false)
        const timer = setInterval(() => void load(false), POLL_MS)
        return () => clearInterval(timer)
      }, [load])

      // пока идёт проверка, опрашиваем чаще: иначе карточка покажет старый кэш и
      // старое время ещё двадцать секунд после нажатия «Обновить»
      useEffect(() => {
        if (!payload?.refreshing) return undefined
        const timer = setInterval(() => void load(false), 1500)
        return () => clearInterval(timer)
      }, [payload?.refreshing, load])

      useEffect(() => {
        if (!open) return undefined
        void load(false)
        const onDown = (event) => {
          if (rootRef.current && !rootRef.current.contains(event.target)) setOpen(false)
        }
        const onKey = (event) => {
          if (event.key === 'Escape') setOpen(false)
        }
        document.addEventListener('mousedown', onDown)
        document.addEventListener('keydown', onKey)
        return () => {
          document.removeEventListener('mousedown', onDown)
          document.removeEventListener('keydown', onKey)
        }
      }, [open, load])

      const status = readStatus(payload)
      const worst = failed ? 'dead' : payload ? status.worst : 'skipped'
      const summary = failingSummary(status)

      return h(
        'div',
        { ref: rootRef, style: { position: 'relative', display: 'inline-flex' } },
        h(
          'button',
          {
            type: 'button',
            onClick: () => setOpen((value) => !value),
            title: failed ? `MCP: ${failed}` : `MCP: ${summary}`,
            'aria-haspopup': 'dialog',
            'aria-expanded': open,
            style: {
              display: 'inline-flex',
              alignItems: 'center',
              gap: '6px',
              height: '24px',
              padding: '0 8px',
              fontSize: '11px',
              color: T.text,
              background: open ? T.hover : 'transparent',
              border: `1px solid ${T.border}`,
              borderRadius: '6px',
              cursor: 'pointer',
            },
          },
          h(Dot, { state: worst }),
          'MCP',
        ),
        open ? h(Panel, { status, onRefresh: () => void load(true), refreshing }) : null,
      )
    }

    function failingSummary(status) {
      if (!status.servers.length) return 'нет данных'
      const dead = status.servers.filter((item) => item.state === 'dead')
      if (dead.length) return `${dead.length} не отвечают: ${dead.map((item) => item.name).join(', ')}`
      return `${status.servers.length} серверов отвечают`
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('conversation.session.header.utilities', () =>
          ctx.slots.register(
            {
              name: 'conversation.session.header.utilities',
              id: 'mcp-status',
              order: 90,
            },
            McpStatusButton,
          ),
        )
      },
    }
  },
})
