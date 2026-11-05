/**
 * Local persistence: transcripts in IndexedDB, preferences in localStorage.
 *
 * The phone is the thing that goes into a tunnel, so the thread you were
 * reading has to survive a cold start with no network. The server stays the
 * source of truth; `reconcile` merges what the store holds with what the
 * sessions API returned, and a message the store queued while offline is sent
 * once, on reconnect, marked as such.
 */

export type Role = 'user' | 'agent'

export interface Message {
  id: string
  sessionId: string
  role: Role
  text: string
  at: string
  /** True while the message exists only on this device. */
  pending?: boolean
}

export interface Thread {
  id: string
  title: string
  updatedAt: string
  preview: string
}

const DB_NAME = 'talos-messaging'
const DB_VERSION = 1
const MESSAGES = 'messages'
const THREADS = 'threads'

function request<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error ?? new Error('indexeddb request failed'))
  })
}

export class TranscriptStore {
  private db: Promise<IDBDatabase> | null = null

  constructor(private readonly factory: IDBFactory | undefined = globalThis.indexedDB) {}

  private open(): Promise<IDBDatabase> {
    if (!this.factory) return Promise.reject(new Error('indexeddb unavailable'))
    this.db ??= new Promise((resolve, reject) => {
      const open = this.factory!.open(DB_NAME, DB_VERSION)
      open.onupgradeneeded = () => {
        const db = open.result
        if (!db.objectStoreNames.contains(MESSAGES)) {
          db.createObjectStore(MESSAGES, { keyPath: 'id' })
        }
        if (!db.objectStoreNames.contains(THREADS)) {
          db.createObjectStore(THREADS, { keyPath: 'id' })
        }
      }
      open.onsuccess = () => resolve(open.result)
      open.onerror = () => reject(open.error ?? new Error('indexeddb open failed'))
    })
    return this.db
  }

  /** Every message for one conversation, oldest first. */
  async messages(sessionId: string): Promise<Message[]> {
    const db = await this.open()
    const all = await request<Message[]>(db.transaction(MESSAGES, 'readonly').objectStore(MESSAGES).getAll())
    return all.filter((message) => message.sessionId === sessionId).sort((a, b) => a.at.localeCompare(b.at))
  }

  async append(message: Message): Promise<void> {
    const db = await this.open()
    await request(db.transaction(MESSAGES, 'readwrite').objectStore(MESSAGES).put(message))
  }

  /** Messages the store holds that never reached the server, oldest first. */
  async pending(): Promise<Message[]> {
    const db = await this.open()
    const all = await request<Message[]>(db.transaction(MESSAGES, 'readonly').objectStore(MESSAGES).getAll())
    return all.filter((message) => message.pending).sort((a, b) => a.at.localeCompare(b.at))
  }

  async threads(): Promise<Thread[]> {
    const db = await this.open()
    const all = await request<Thread[]>(db.transaction(THREADS, 'readonly').objectStore(THREADS).getAll())
    return all.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
  }

  async putThread(thread: Thread): Promise<void> {
    const db = await this.open()
    await request(db.transaction(THREADS, 'readwrite').objectStore(THREADS).put(thread))
  }
}

/** Preferences survive a cold start; they are a UI choice, not agent state. */
export function readPreference(storage: Pick<Storage, 'getItem'>, key: string): string | null {
  return storage.getItem(`talos-messaging:${key}`)
}

export function writePreference(storage: Pick<Storage, 'setItem'>, key: string, value: string): void {
  storage.setItem(`talos-messaging:${key}`, value)
}

/**
 * Merge what the device holds with what the server returned.
 *
 * - a server message wins over a local one with the same id (the server's copy
 *   is the one that was actually said);
 * - a local message the server does not know is kept, so an offline send is not
 *   silently lost;
 * - a pending local message whose text the server *does* hold (same session,
 *   same text, same role) is dropped: it was resent and acknowledged.
 */
export function reconcile(local: Message[], remote: Message[]): Message[] {
  const byId = new Map<string, Message>()
  for (const message of local) byId.set(message.id, message)
  for (const message of remote) byId.set(message.id, message)
  const merged = [...byId.values()]
  const acknowledged = (message: Message): boolean =>
    Boolean(message.pending) &&
    remote.some(
      (other) => !other.pending && other.sessionId === message.sessionId && other.role === message.role && other.text === message.text,
    )
  return merged
    .filter((message) => !acknowledged(message))
    .sort((a, b) => a.at.localeCompare(b.at))
}

/** A stable id for a message this device is composing. */
export function localMessageId(): string {
  const random = globalThis.crypto?.randomUUID?.()
  return random ? `local-${random}` : `local-${Date.now()}-${Math.floor(Math.random() * 1e6)}`
}
