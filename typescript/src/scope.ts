import { blake2bHex } from "./digest.ts";

export const SCOPE_TAG_LENGTH = 32;

export const SCOPE_META_KEYS: readonly string[] = [
  "conversationId",
  "conversation_id",
  "sessionId",
  "session_id",
  "threadId",
  "thread_id",
];

export interface DerivedScope {
  readonly tag: string;
  readonly fromClient: boolean;
}

export function mint(): string {
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(SCOPE_TAG_LENGTH / 2));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function fromConversationId(conversationId: string): string {
  return blake2bHex(conversationId, 16).slice(0, SCOPE_TAG_LENGTH);
}

function lookup(meta: unknown, key: string): unknown {
  if (meta instanceof Map) {
    return meta.get(key);
  }
  if (typeof meta === "object" && meta !== null && !Array.isArray(meta) && Object.hasOwn(meta, key)) {
    return (meta as Readonly<Record<string, unknown>>)[key];
  }
  return undefined;
}

function conversationId(meta: unknown): string | null {
  for (const key of SCOPE_META_KEYS) {
    const value = lookup(meta, key);
    if (typeof value === "string" && value !== "") {
      return value;
    }
  }
  return null;
}

export function derive(meta: unknown): DerivedScope {
  const candidate = conversationId(meta);
  if (candidate !== null) {
    return { tag: fromConversationId(candidate), fromClient: true };
  }
  return { tag: mint(), fromClient: false };
}
