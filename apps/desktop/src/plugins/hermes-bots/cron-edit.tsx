/**
 * Edit an existing Bot Mode routine. The Routines pane could create, pause and delete a
 * routine but never change one: `cron.manage` had no update action and its list carried only a
 * 100-char prompt preview. Every field the create dialog sets (plus model, workdir, skills) is
 * editable here, and only the fields the user changed are sent.
 */

import {
  Button,
  Checkbox,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  host,
  Input,
  Textarea,
  translateNow,
  useI18n,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { BOT_TAG_RE, invalidateRoutineOwner, routineInputError, routinePrompt, SAFE_ROUTINE_MARKER } from './cron'
import { labeled } from './dialog-parts'
import { useBots } from './i18n'
import { requestForBot } from './routing'
import type { RosterRow, RoutineJob } from './types'

const SCHEDULED_PREFIX = '[Scheduled routine] '

/** Read one shell single-quoted word starting at `start` (the opening quote). */
function readShellQuoted(text: string, start: number): null | string {
  let out = ''
  let i = start

  while (text[i] === "'") {
    const close = text.indexOf("'", i + 1)

    if (close < 0) {
      return null
    }

    out += text.slice(i + 1, close)
    i = close + 1

    // routinePrompt's shellQuote escapes ' as '"'"' — a closed quote, "'", then a reopened one.
    if (text.startsWith(`"'"'`, i)) {
      out += "'"
      i += 3
    } else {
      break
    }
  }

  return out
}

/** The user's instruction inside a stored prompt: delegated routines wrap it in a
 *  `hermes -p … -q '[Scheduled routine] …'` command that must not be edited as text. */
export function routineInstruction(prompt: string): string {
  if (!prompt.startsWith(SAFE_ROUTINE_MARKER)) {
    return prompt
  }

  const at = prompt.lastIndexOf(" -q '")
  const word = at >= 0 ? readShellQuoted(prompt, at + 4) : null

  if (word === null) {
    return prompt
  }

  return word.startsWith(SCHEDULED_PREFIX) ? word.slice(SCHEDULED_PREFIX.length) : word
}

/** The list sends repeat as display text ("forever", "once", "5 times", "2/5"). */
export function routineRepeatTimes(repeat: RoutineJob['repeat']): string {
  if (typeof repeat === 'number') {
    return repeat > 0 ? String(repeat) : ''
  }

  const text = String(repeat ?? '').trim()

  if (text === 'once') {
    return '1'
  }

  const match = /(\d+)\s*times$/.exec(text) || /\/(\d+)$/.exec(text)

  return match ? match[1] : ''
}

interface RoutineForm {
  continuity: boolean
  deliver: string
  instruction: string
  model: string
  repeat: string
  schedule: string
  skills: string
  title: string
  workdir: string
}

export function routineFormFromJob(job: RoutineJob): RoutineForm {
  return {
    continuity: Boolean(job.continuity),
    deliver: job.deliver || 'local',
    instruction: routineInstruction(String(job.prompt ?? job.prompt_preview ?? '')),
    model: job.model || '',
    repeat: routineRepeatTimes(job.repeat),
    schedule: job.schedule || '',
    skills: (job.skills || []).join(', '),
    title: (job.name || '').replace(BOT_TAG_RE, ''),
    workdir: job.workdir || ''
  }
}

/** `cron.manage update` params for the fields that changed (`''` clears an optional one). */
export function routineUpdateParams(
  job: RoutineJob,
  before: RoutineForm,
  after: RoutineForm,
  profile: string,
  activeProfile: string
): Record<string, unknown> {
  const params: Record<string, unknown> = {}
  const changed = (key: keyof RoutineForm) => before[key] !== after[key]
  const tag = BOT_TAG_RE.exec(job.name || '')?.[0] ?? `[bot:${profile}] `

  if (changed('title')) {
    params.name = `${tag.trimEnd()} ${after.title.trim()}`
  }

  // The delegated wrapper embeds the title too, so a rename rewrites the prompt as well.
  const wrapped = String(job.prompt || '').startsWith(SAFE_ROUTINE_MARKER)

  if (changed('instruction') || (wrapped && changed('title'))) {
    params.prompt = routinePrompt(profile, after.title.trim(), after.instruction.trim(), activeProfile)
  }

  if (changed('schedule')) {
    params.schedule = after.schedule.trim()
  }

  if (changed('repeat')) {
    params.repeat = after.repeat.trim() || 0
  }

  if (changed('deliver')) {
    params.deliver = after.deliver.trim() || 'local'
  }

  for (const key of ['model', 'workdir'] as const) {
    if (changed(key)) {
      params[key] = after[key].trim()
    }
  }

  if (changed('skills')) {
    params.skills = after.skills
      .split(',')
      .map(item => item.trim())
      .filter(Boolean)
  }

  if (changed('continuity')) {
    params.continuity = after.continuity
  }

  return params
}

interface EditRoutineDialogProps {
  job: null | RoutineJob
  onClose: () => void
  owner: RosterRow
}

export function EditRoutineDialog({ job, onClose, owner }: EditRoutineDialogProps) {
  const b = useBots()
  const { t } = useI18n()
  const c = b.cron
  const activeProfile = useValue(host.state.profile)
  const profile = owner.name
  const [initial, setInitial] = useState<null | RoutineForm>(null)
  const [form, setForm] = useState<null | RoutineForm>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)

  // Seed once per opened job: the 20s poll replaces the job object, and re-seeding on it would
  // wipe what the user is typing.
  const jobId = job?.job_id

  useEffect(() => {
    if (!job) {
      setInitial(null)
      setForm(null)

      return
    }

    const seeded = routineFormFromJob(job)

    setInitial(seeded)
    setForm(seeded)
    setError(null)
    setBusy(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId])

  if (!job || !form || !initial) {
    return null
  }

  const set = <K extends keyof RoutineForm>(key: K, value: RoutineForm[K]) => setForm({ ...form, [key]: value })

  const submit = async () => {
    const inputError =
      routineInputError(form.title, form.instruction, c) ||
      (!form.title.trim() || !form.instruction.trim() || !form.schedule.trim() ? c.editDescription : null)

    if (inputError) {
      setError(inputError)

      return
    }

    const params = routineUpdateParams(job, initial, form, profile, activeProfile)

    if (!Object.keys(params).length) {
      onClose()

      return
    }

    setBusy(true)
    setError(null)

    try {
      const result = (await requestForBot(owner, 'cron.manage', {
        action: 'update',
        job_id: job.job_id,
        ...(profile ? { profile } : {}),
        ...params
      })) as { error?: string; success?: boolean } | undefined

      if (result && result.success === false) {
        throw new Error(result.error || t.common.saving)
      }

      await invalidateRoutineOwner(owner)
      host.notify({ kind: 'success', message: `${c.updated}: ${form.title.trim()}` })
      onClose()
    } catch (err) {
      setBusy(false)
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const text = (key: 'deliver' | 'model' | 'repeat' | 'schedule' | 'skills' | 'title' | 'workdir', hint?: string) => (
    <>
      <Input
        className={key === 'schedule' || key === 'workdir' ? 'font-mono' : undefined}
        id={`routine-edit-${key}`}
        onChange={event => set(key, event.target.value)}
        value={form[key]}
      />
      {hint ? <div className="text-[0.65rem] text-(--ui-text-quaternary)">{hint}</div> : null}
    </>
  )

  return (
    <Dialog onOpenChange={value => !value && !busy && onClose()} open>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{c.editTitle}</DialogTitle>
          <DialogDescription>{c.editDescription}</DialogDescription>
        </DialogHeader>
        <div className="grid max-h-[60vh] gap-3 overflow-y-auto">
          {labeled(t.cron.nameLabel, text('title'))}
          {labeled(
            t.cron.promptLabel,
            <Textarea
              className="min-h-28"
              id="routine-edit-instruction"
              onChange={event => set('instruction', event.target.value)}
              value={form.instruction}
            />
          )}
          {labeled(c.schedule, text('schedule', c.scheduleHint))}
          {labeled(c.repeat, text('repeat', c.repeatHint))}
          {labeled(translateNow('cron.deliverLabel'), text('deliver'))}
          {labeled(translateNow('cron.modelLabel'), text('model'))}
          {labeled(c.workdir, text('workdir'))}
          {labeled(c.skillsLabel, text('skills'))}
          <label className="flex cursor-pointer items-center gap-2 text-xs text-(--ui-text-tertiary) select-none">
            <Checkbox checked={form.continuity} onCheckedChange={value => set('continuity', Boolean(value))} />
            {c.continuity}
          </label>
          {error ? (
            <div className="rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-xs text-(--ui-accent)">
              {error}
            </div>
          ) : null}
        </div>
        <DialogFooter>
          <Button disabled={busy} onClick={onClose} variant="ghost">
            {t.common.cancel}
          </Button>
          <Button disabled={busy} onClick={submit}>
            {busy ? t.common.saving : t.cron.saveChanges}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
