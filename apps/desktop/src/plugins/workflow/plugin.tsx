/**
 * Workflow — a status-bar "Workflow" button and a `/workflow` editor page for
 * saved multi-step workflows (`<HERMES_HOME>/workflows/*.yaml`). The backend
 * owns the files (`workflow.list` / `workflow.save`, `agent/workflow.py`);
 * Run submits `/workflow <name> <input>` through the chat composer, so it runs
 * exactly like the user typing the slash command.
 */

import {
  Button,
  cn,
  Codicon,
  type HermesPlugin,
  host,
  Input,
  PALETTE_AREA,
  type PaletteContribution,
  type RouteContribution,
  ROUTES_AREA,
  STATUSBAR_AREAS,
  Textarea
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useState } from 'react'

import { runWorkflowInChat, WORKFLOW_TEMPLATE, workflowCommand, type WorkflowRow } from './run'

const ROUTE = '/workflow'

interface ListResult {
  workflows: WorkflowRow[]
  dir: string
}

function WorkflowButton() {
  return (
    <button
      aria-label="Workflow"
      className={cn(
        'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      )}
      data-testid="workflow-button"
      onClick={() => host.navigate(ROUTE)}
      type="button"
    >
      <Codicon name="checklist" size="0.7rem" />
      <span>Workflow</span>
    </button>
  )
}

function WorkflowEditorPage() {
  const [rows, setRows] = useState<WorkflowRow[]>([])
  const [dir, setDir] = useState('')
  const [name, setName] = useState('hello-workflow')
  const [source, setSource] = useState(WORKFLOW_TEMPLATE)
  const [input, setInput] = useState('')
  const [status, setStatus] = useState<{ error: boolean; text: string } | null>(null)

  const refresh = useCallback(async () => {
    const res = await host.request<ListResult>('workflow.list')
    setRows(res.workflows)
    setDir(res.dir)

    return res.workflows
  }, [])

  useEffect(() => {
    refresh()
      .then(list => {
        if (list[0]) {
          setName(list[0].name)
          setSource(list[0].source)
        }
      })
      .catch(err => setStatus({ error: true, text: String(err?.message ?? err) }))
  }, [refresh])

  const save = async () => {
    try {
      const res = await host.request<{ name: string; steps: number }>('workflow.save', { name, source })
      setStatus({ error: false, text: `Saved ${res.name} (${res.steps} steps)` })
      await refresh()

      return true
    } catch (err) {
      setStatus({ error: true, text: String((err as Error)?.message ?? err) })

      return false
    }
  }

  const run = async () => {
    if (!(await save())) {
      return
    }

    setStatus({ error: false, text: `Running ${workflowCommand(name, input)}` })

    if (!(await runWorkflowInChat(name, input))) {
      setStatus({ error: true, text: 'No chat composer answered; open a chat and try again.' })
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-auto p-4" data-testid="workflow-editor">
      <div className="flex items-center gap-2">
        <Codicon name="checklist" />
        <h1 className="text-base font-semibold">Workflows</h1>
        <span className="text-xs text-(--ui-text-tertiary)">{dir}</span>
      </div>
      <div className="flex min-h-0 flex-1 gap-4">
        <ul className="w-48 shrink-0 space-y-1 text-sm" data-testid="workflow-list">
          {rows.map(row => (
            <li key={row.name}>
              <button
                className={cn(
                  'w-full rounded px-2 py-1 text-left hover:bg-(--chrome-action-hover)',
                  row.name === name && 'bg-(--chrome-action-hover)'
                )}
                onClick={() => {
                  setName(row.name)
                  setSource(row.source)
                }}
                type="button"
              >
                <div>{row.name}</div>
                <div className="text-xs text-(--ui-text-tertiary)">{row.error ? 'invalid' : `${row.steps} steps`}</div>
              </button>
            </li>
          ))}
          <li>
            <button
              className="w-full rounded px-2 py-1 text-left text-xs text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)"
              onClick={() => {
                setName('new-workflow')
                setSource(WORKFLOW_TEMPLATE)
              }}
              type="button"
            >
              + New workflow
            </button>
          </li>
        </ul>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <Input aria-label="Workflow name" onChange={e => setName(e.target.value)} value={name} />
          <Textarea
            aria-label="Workflow YAML"
            className="min-h-64 flex-1 font-mono text-xs"
            onChange={e => setSource(e.target.value)}
            value={source}
          />
          <Input
            aria-label="Workflow input"
            onChange={e => setInput(e.target.value)}
            placeholder="Input (optional)"
            value={input}
          />
          <div className="flex items-center gap-2">
            <Button onClick={save} size="sm" variant="outline">
              Save
            </Button>
            <Button data-testid="workflow-run" onClick={run} size="sm">
              Run in chat
            </Button>
            {status && (
              <span className={cn('text-xs', status.error ? 'text-destructive' : 'text-(--ui-text-tertiary)')}>
                {status.text}
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

const plugin: HermesPlugin = {
  id: 'workflow',
  name: 'Workflow',
  description: 'Edit saved multi-step workflows and run them in chat with /workflow.',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE } satisfies RouteContribution,
        render: () => <WorkflowEditorPage />
      },
      {
        id: 'button',
        area: STATUSBAR_AREAS.left,
        order: 90,
        render: () => <WorkflowButton />
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'workflow.open',
          label: 'Open workflow editor',
          keywords: ['workflow', 'steps', 'automation'],
          run: () => host.navigate(ROUTE)
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
