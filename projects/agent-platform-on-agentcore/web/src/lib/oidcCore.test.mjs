import { test } from "node:test";
import assert from "node:assert/strict";
import {
  base64UrlEncode,
  sha256Challenge,
  sha256Bytes,
  buildAuthorizeUrl,
  parseCallbackParams,
} from "./oidcCore.mjs";

test("base64UrlEncode is url-safe and unpadded", () => {
  const out = base64UrlEncode(new Uint8Array([251, 255, 191]));
  assert.ok(!out.includes("+") && !out.includes("/") && !out.includes("="));
});

test("sha256Challenge matches the RFC 7636 test vector", async () => {
  // RFC 7636 Appendix B
  const verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
  const challenge = await sha256Challenge(verifier);
  assert.equal(challenge, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
});

test("the pure-JS fallback digests like crypto.subtle", async () => {
  // The plain-HTTP ALB has no crypto.subtle; the fallback must produce the
  // identical challenge or the token exchange fails with invalid_grant.
  const verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
  const fallback = base64UrlEncode(sha256Bytes(new TextEncoder().encode(verifier)));
  assert.equal(fallback, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  // And on an input longer than one 64-byte block.
  const long = "x".repeat(200);
  const viaSubtle = base64UrlEncode(
    new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(long)))
  );
  assert.equal(base64UrlEncode(sha256Bytes(new TextEncoder().encode(long))), viaSubtle);
});

test("buildAuthorizeUrl includes the required params", () => {
  const url = new URL(
    buildAuthorizeUrl({
      authorizationEndpoint: "https://idp/authorize",
      clientId: "c1",
      redirectUri: "https://app/auth/callback",
      scopes: "openid profile email",
      state: "st",
      nonce: "no",
      codeChallenge: "ch",
    })
  );
  assert.equal(url.origin + url.pathname, "https://idp/authorize");
  assert.equal(url.searchParams.get("response_type"), "code");
  assert.equal(url.searchParams.get("client_id"), "c1");
  assert.equal(url.searchParams.get("redirect_uri"), "https://app/auth/callback");
  assert.equal(url.searchParams.get("scope"), "openid profile email");
  assert.equal(url.searchParams.get("state"), "st");
  assert.equal(url.searchParams.get("nonce"), "no");
  assert.equal(url.searchParams.get("code_challenge"), "ch");
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  // Force the account picker every time; otherwise Entra silently reuses the
  // browser session and nobody can switch accounts.
  assert.equal(url.searchParams.get("prompt"), "select_account");
});

test("parseCallbackParams reads code/state/error", () => {
  assert.deepEqual(parseCallbackParams("?code=abc&state=xyz"), {
    code: "abc",
    state: "xyz",
    error: null,
  });
  assert.equal(parseCallbackParams("?error=access_denied").error, "access_denied");
});
