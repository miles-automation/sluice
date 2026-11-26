import { blake2b } from "@noble/hashes/blake2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

const ENCODER = new TextEncoder();

export function utf8(text: string): Uint8Array {
  if (!text.isWellFormed()) {
    throw new TypeError("text contains a lone surrogate and has no UTF-8 encoding");
  }
  return ENCODER.encode(text);
}

export function blake2bHex(text: string, digestBytes: number): string {
  return bytesToHex(blake2b(utf8(text), { dkLen: digestBytes }));
}
