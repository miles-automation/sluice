import { deserialize, serialize } from "node:v8";

export const BREACH_EXIT = 86;
export const HEADER_BYTES = 4;
export const MAX_FRAME_BYTES = 2 ** 31 - 1;

export type Message = Readonly<Record<string, unknown>>;

export function frame(message: Message): Buffer {
  const body = serialize(message);
  if (body.length > MAX_FRAME_BYTES) {
    throw new RangeError(`message of ${body.length} bytes exceeds the ${MAX_FRAME_BYTES} byte frame`);
  }
  const header = Buffer.allocUnsafe(HEADER_BYTES);
  header.writeUInt32BE(body.length, 0);
  return Buffer.concat([header, body]);
}

export class FrameReader {
  readonly #maxBytes: number;
  #chunks: Buffer[] = [];
  #buffered = 0;

  constructor(maxBytes: number = MAX_FRAME_BYTES) {
    this.#maxBytes = maxBytes;
  }

  push(chunk: Buffer): Message[] {
    this.#chunks.push(chunk);
    this.#buffered += chunk.length;
    const messages: Message[] = [];
    for (;;) {
      if (this.#buffered < HEADER_BYTES) {
        return messages;
      }
      const head = this.#take(HEADER_BYTES, false);
      const size = head.readUInt32BE(0);
      if (size > this.#maxBytes) {
        throw new RangeError(`frame of ${size} bytes exceeds the ${this.#maxBytes} byte limit`);
      }
      if (this.#buffered < HEADER_BYTES + size) {
        return messages;
      }
      this.#take(HEADER_BYTES, true);
      const decoded: unknown = deserialize(this.#take(size, true));
      if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) {
        throw new TypeError("frame is not an object");
      }
      messages.push(decoded as Message);
    }
  }

  #take(size: number, consume: boolean): Buffer {
    const first = this.#chunks[0];
    let joined: Buffer;
    if (first !== undefined && first.length >= size) {
      joined = first;
    } else {
      joined = Buffer.concat(this.#chunks);
      this.#chunks = [joined];
    }
    const taken = joined.subarray(0, size);
    if (consume) {
      const rest = joined.subarray(size);
      this.#chunks[0] = rest;
      if (rest.length === 0) {
        this.#chunks.shift();
      }
      this.#buffered -= size;
    }
    return taken;
  }
}
