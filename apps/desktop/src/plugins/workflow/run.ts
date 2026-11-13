import { host } from '@hermes/plugin-sdk'

export interface WorkflowRow {
  name: string
  description: string
  steps: number
  source: string
  error: null | string
}

export const WORKFLOW_TEMPLATE = `description: Say hello in two steps
steps:
  - name: Greet
    prompt: Reply with a one-line greeting for {input}.
  - name: Sign off
    prompt: Reply with a one-line sign-off.
`

export const workflowCommand = (name: string, input: string) =>
  `/workflow ${name.trim()}${input.trim() ? ` ${input.trim()}` : ''}`

/** Open a fresh chat and submit the slash command through its composer. The
 *  composer mounts after navigation, so retry briefly until one claims it. */
export async function runWorkflowInChat(name: string, input: string, attempts = 20): Promise<boolean> {
  host.navigate('/')

  for (let i = 0; i < attempts; i += 1) {
    if (host.composer.submit(null, workflowCommand(name, input))) {
      return true
    }

    await new Promise(resolve => setTimeout(resolve, 100))
  }

  return false
}
