/**
 * OIDC Authorization Code + PKCE, the pure part (no browser APIs; node --test).
 * fetch / sessionStorage / location live in oidc.ts.
 */

/** Uint8Array → base64url (unpadded). */
export function base64UrlEncode(bytes) {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  const base64 =
    typeof btoa === "function"
      ? btoa(binary)
      : Buffer.from(binary, "binary").toString("base64");
  return base64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * PKCE S256: challenge = base64url(sha256(verifier)).
 *
 * `crypto.subtle` exists only in a secure context (HTTPS or localhost), so a
 * plain-HTTP ALB origin has none — the same gap that took out
 * crypto.randomUUID elsewhere in this app. Fall back to a pure-JS SHA-256 so
 * PKCE keeps working there; the digest is identical.
 */
export async function sha256Challenge(verifier) {
  const data = new TextEncoder().encode(verifier);
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  const digest = subtle
    ? new Uint8Array(await subtle.digest("SHA-256", data))
    : sha256Bytes(data);
  return base64UrlEncode(digest);
}

/** Pure-JS SHA-256 (FIPS 180-4), bytes in → 32 bytes out. Fallback only. */
export function sha256Bytes(msg) {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const H = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);

  const l = msg.length;
  const bitLen = l * 8;
  // Pad to a multiple of 64 with room for 0x80 and the 64-bit length.
  const total = (Math.floor((l + 8) / 64) + 1) * 64;
  const m = new Uint8Array(total);
  m.set(msg);
  m[l] = 0x80;
  const hi = Math.floor(bitLen / 0x100000000);
  const lo = bitLen >>> 0;
  m[total - 8] = (hi >>> 24) & 0xff;
  m[total - 7] = (hi >>> 16) & 0xff;
  m[total - 6] = (hi >>> 8) & 0xff;
  m[total - 5] = hi & 0xff;
  m[total - 4] = (lo >>> 24) & 0xff;
  m[total - 3] = (lo >>> 16) & 0xff;
  m[total - 2] = (lo >>> 8) & 0xff;
  m[total - 1] = lo & 0xff;

  const w = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));

  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) {
      const j = off + i * 4;
      w[i] = (m[j] << 24) | (m[j + 1] << 16) | (m[j + 2] << 8) | m[j + 3];
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }

    let a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }

    H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
    H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
  }

  const out = new Uint8Array(32);
  for (let i = 0; i < 8; i++) {
    out[i * 4] = (H[i] >>> 24) & 0xff;
    out[i * 4 + 1] = (H[i] >>> 16) & 0xff;
    out[i * 4 + 2] = (H[i] >>> 8) & 0xff;
    out[i * 4 + 3] = H[i] & 0xff;
  }
  return out;
}

/** The authorize URL. */
export function buildAuthorizeUrl({
  authorizationEndpoint,
  clientId,
  redirectUri,
  scopes,
  state,
  nonce,
  codeChallenge,
}) {
  const url = new URL(authorizationEndpoint);
  const params = {
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: scopes,
    state,
    nonce,
    code_challenge: codeChallenge,
    code_challenge_method: "S256",
    // Always show the account picker. Without it Entra silently reuses the
    // browser's existing session and bounces straight back, so a user can
    // never sign in as a different account.
    prompt: "select_account",
  };
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  return url.toString();
}

/** The callback query string. */
export function parseCallbackParams(search) {
  const params = new URLSearchParams(search);
  return {
    code: params.get("code"),
    state: params.get("state"),
    error: params.get("error"),
  };
}

/**
 * Where to ask the server for this deployment's login options.
 *
 * The origin rides along as a query parameter rather than being left to the
 * Origin header: deployed, this is a same-origin GET through the Next proxy, and
 * browsers attach an Origin header only to cross-origin or non-GET requests, so
 * the server never saw one and always answered with its first registered
 * redirect_uri. A dev server sharing production's app registration then hit
 * the IdP's redirect_uri mismatch. Server-rendered code has no origin to send
 * and gets the plain URL.
 */
export function authConfigUrl(apiBase, origin) {
  const base = `${apiBase ?? ""}/api/auth/config`;
  return origin ? `${base}?origin=${encodeURIComponent(origin)}` : base;
}
