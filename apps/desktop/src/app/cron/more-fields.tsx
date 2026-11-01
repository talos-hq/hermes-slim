import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldHint } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import type { CronJob, CronJobUpdates } from '@/hermes'
import type { Translations } from '@/i18n'

/** Every job field the update route accepts beyond the editor's core ones (name, prompt,
 *  schedule, delivery, model, catch-up). Edited as text; coerced only when saved. */
export interface MoreFieldsState {
  baseUrl: string
  contextFrom: string
  failureDeliver: string
  noAgent: boolean
  reasoningEffort: string
  repeat: string
  script: string
  skills: string
  timezone: string
  toolsets: string
  workdir: string
}

const text = (value: unknown): string => (typeof value === 'string' ? value : '')

const list = (value: unknown): string =>
  Array.isArray(value) ? value.map(String).join(', ') : typeof value === 'string' ? value : ''

const splitList = (value: string): string[] =>
  value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)

/** `self` / the job's own id is the editor's continuity switch, not an external ref. */
function externalContext(job: CronJob | null): string {
  const refs = Array.isArray(job?.context_from) ? job.context_from : []

  return list(refs.filter(ref => ref !== 'self' && ref !== job?.id))
}

export function moreFieldsFromJob(job: CronJob | null): MoreFieldsState {
  const times = job?.repeat && typeof job.repeat === 'object' ? job.repeat.times : null

  return {
    baseUrl: text(job?.base_url),
    contextFrom: externalContext(job),
    failureDeliver: list(job?.failure_deliver),
    noAgent: Boolean(job?.no_agent),
    reasoningEffort: text(job?.reasoning_effort),
    repeat: typeof times === 'number' && times > 0 ? String(times) : '',
    script: text(job?.script),
    skills: list(job?.skills?.length ? job.skills : job?.skill ? [job.skill] : []),
    timezone: text(job?.timezone),
    toolsets: list(job?.enabled_toolsets),
    workdir: text(job?.workdir)
  }
}

/** Only the fields the user changed: an untouched field is never re-sent, so an edit cannot
 *  rewrite (or re-validate) a value it did not touch. `''` clears an optional field. */
export function moreFieldsUpdates(
  initial: MoreFieldsState,
  next: MoreFieldsState,
  job: CronJob | null
): CronJobUpdates {
  const updates: CronJobUpdates = {}
  const changed = (key: keyof MoreFieldsState) => initial[key] !== next[key]

  if (changed('skills')) {
    updates.skills = splitList(next.skills)
  }

  if (changed('repeat')) {
    const n = parseInt(next.repeat.trim(), 10)
    // Blank / 0 = forever (normalize_repeat_value treats 0 as unlimited).
    updates.repeat = Number.isFinite(n) && n > 0 ? n : 0
  }

  if (changed('workdir')) {
    updates.workdir = next.workdir.trim()
  }

  if (changed('toolsets')) {
    // Blank restores the unrestricted default; [] would be an explicit zero-tool allowlist.
    updates.enabled_toolsets = next.toolsets.trim() ? splitList(next.toolsets) : null
  }

  if (changed('script')) {
    updates.script = next.script.trim()
  }

  if (changed('noAgent')) {
    updates.no_agent = next.noAgent
  }

  if (changed('baseUrl')) {
    updates.base_url = next.baseUrl.trim()
  }

  if (changed('reasoningEffort')) {
    updates.reasoning_effort = next.reasoningEffort.trim()
  }

  if (changed('contextFrom')) {
    // Keep the job's own continuity ref: this field only edits the external ones.
    const own = (Array.isArray(job?.context_from) ? job.context_from : []).filter(
      ref => ref === 'self' || ref === job?.id
    )

    updates.context_from = [...own, ...splitList(next.contextFrom)]
  }

  if (changed('failureDeliver')) {
    updates.failure_deliver = next.failureDeliver.trim()
  }

  if (changed('timezone')) {
    updates.timezone = next.timezone.trim()
  }

  return updates
}

interface MoreFieldsProps {
  c: Translations['cron']
  onChange: (next: MoreFieldsState) => void
  value: MoreFieldsState
}

const TEXT_FIELDS: Array<{ key: Exclude<keyof MoreFieldsState, 'noAgent'>; list?: boolean; mono?: boolean }> = [
  { key: 'skills', list: true },
  { key: 'repeat' },
  { key: 'workdir', mono: true },
  { key: 'toolsets', list: true },
  { key: 'script', mono: true },
  { key: 'contextFrom', list: true },
  { key: 'failureDeliver', list: true },
  { key: 'reasoningEffort' },
  { key: 'baseUrl', mono: true },
  { key: 'timezone' }
]

/** The rest of a job's fields, folded so the common edit (title, prompt, frequency) stays short. */
export function MoreFields({ c, onChange, value }: MoreFieldsProps) {
  const m = c.more

  return (
    <details className="rounded-md border border-(--ui-stroke-secondary) px-3 py-2" data-slot="cron-more-fields">
      <summary className="cursor-pointer select-none text-xs font-medium text-muted-foreground">{m.title}</summary>
      <div className="mt-3 grid gap-3">
        {TEXT_FIELDS.map(field => (
          <Field htmlFor={`cron-more-${field.key}`} key={field.key} label={m.labels[field.key]}>
            <Input
              className={field.mono ? 'font-mono' : undefined}
              id={`cron-more-${field.key}`}
              inputMode={field.key === 'repeat' ? 'numeric' : undefined}
              onChange={event => onChange({ ...value, [field.key]: event.target.value })}
              value={value[field.key]}
            />
            {field.list ? <FieldHint>{m.listHint}</FieldHint> : null}
            {field.key === 'repeat' ? <FieldHint>{m.repeatHint}</FieldHint> : null}
          </Field>
        ))}
        <label className="flex cursor-pointer items-center gap-2 text-xs select-none">
          <Checkbox
            checked={value.noAgent}
            id="cron-more-noAgent"
            onCheckedChange={checked => onChange({ ...value, noAgent: Boolean(checked) })}
          />
          {m.labels.noAgent}
        </label>
      </div>
    </details>
  )
}
