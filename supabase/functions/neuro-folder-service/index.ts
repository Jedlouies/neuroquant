import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const HASH_PATTERN = /^[a-f0-9]{64}$/i;
let serviceAccountCache: any = null;
let supabaseClient: any = null;

function getServiceAccount(): any {
  if (serviceAccountCache) return serviceAccountCache;
  const raw = Deno.env.get("FIREBASE_SERVICE_ACCOUNT_JSON");
  if (!raw) fail(500, "configuration", "Set FIREBASE_SERVICE_ACCOUNT_JSON in Supabase Edge Function secrets.");
  try {
    serviceAccountCache = JSON.parse(raw);
  } catch {
    fail(500, "configuration", "FIREBASE_SERVICE_ACCOUNT_JSON must contain the complete, valid service-account JSON.");
  }
  if (!serviceAccountCache?.client_email || !serviceAccountCache?.private_key) {
    fail(500, "configuration", "FIREBASE_SERVICE_ACCOUNT_JSON is missing client_email or private_key.");
  }
  return serviceAccountCache;
}

function getSupabaseClient(): any {
  if (supabaseClient) return supabaseClient;
  const serviceKey = Deno.env.get("NEUROQUANT_SUPABASE_SERVICE_ROLE_KEY");
  if (!serviceKey) fail(500, "configuration", "Set NEUROQUANT_SUPABASE_SERVICE_ROLE_KEY in Supabase Edge Function secrets.");
  supabaseClient = createClient(
    Deno.env.get("SUPABASE_URL") || "https://rgvieraaviomiovcqyyu.supabase.co",
    serviceKey,
    { auth: { persistSession: false, autoRefreshToken: false } },
  );
  return supabaseClient;
}

function getFirebaseProjectId(): string {
  return getServiceAccount().project_id || "striped-helper-465504-d0";
}

let cachedAccessToken = "";
let cachedTokenUntil = 0;

function fail(status: number, code: string, message: string): never {
  throw Object.assign(new Error(message), { status, code });
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function utf8Base64Url(value: string): string {
  return base64Url(new TextEncoder().encode(value));
}

function pemBytes(pem: string): Uint8Array {
  const base64 = pem.replace(/-----BEGIN PRIVATE KEY-----|-----END PRIVATE KEY-----|\s/g, "");
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
}

async function firestoreAccessToken(): Promise<string> {
  const serviceAccount = getServiceAccount();
  if (!serviceAccount.client_email || !serviceAccount.private_key) {
    fail(500, "configuration", "FIREBASE_SERVICE_ACCOUNT_JSON is missing or invalid.");
  }
  if (cachedAccessToken && Date.now() < cachedTokenUntil) return cachedAccessToken;
  const now = Math.floor(Date.now() / 1000);
  const header = utf8Base64Url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const claim = utf8Base64Url(JSON.stringify({
    iss: serviceAccount.client_email,
    scope: "https://www.googleapis.com/auth/datastore",
    aud: "https://oauth2.googleapis.com/token",
    iat: now,
    exp: now + 3600,
  }));
  const unsigned = `${header}.${claim}`;
  const signingKey = await crypto.subtle.importKey(
    "pkcs8", pemBytes(serviceAccount.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"],
  );
  const signature = new Uint8Array(await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5", signingKey, new TextEncoder().encode(unsigned),
  ));
  const assertion = `${unsigned}.${base64Url(signature)}`;
  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion,
    }),
  });
  const result = await response.json();
  if (!response.ok || typeof result.access_token !== "string") {
    console.error("Google token exchange failed", result.error || response.status);
    fail(502, "firestore-auth", "Could not connect to the Firebase project.");
  }
  cachedAccessToken = result.access_token;
  cachedTokenUntil = Date.now() + Math.max(60, Number(result.expires_in || 3600) - 120) * 1000;
  return cachedAccessToken;
}

function documentUrl(collection: string, documentId?: string): string {
  const base = `https://firestore.googleapis.com/v1/projects/${encodeURIComponent(getFirebaseProjectId())}/databases/(default)/documents`;
  return documentId ? `${base}/${collection}/${encodeURIComponent(documentId)}` : `${base}/${collection}`;
}

async function firestoreRequest(url: string, init: RequestInit = {}): Promise<any> {
  const response = await fetch(url, {
    ...init,
    headers: {
      Authorization: `Bearer ${await firestoreAccessToken()}`,
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 404) return null;
  if (!response.ok) {
    console.error("Firestore REST request failed", response.status, body.error?.status);
    fail(502, "firestore", "Could not read or update Firebase folder metadata.");
  }
  return body;
}

function stringField(document: any, field: string): string {
  return document?.fields?.[field]?.stringValue || "";
}

async function readCloudAccount(username: string, password: string): Promise<boolean> {
  const doc = await firestoreRequest(documentUrl("users", username));
  if (!doc) return false;
  const storedHash = stringField(doc, "password_hash");
  const salt = stringField(doc, "salt");
  return await verifyPassword(password, salt, storedHash, 100_000);
}

function hexBytes(hex: string): Uint8Array {
  const bytes = new Uint8Array(hex.length / 2);
  for (let index = 0; index < bytes.length; index++) bytes[index] = Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16);
  return bytes;
}

async function pbkdf2Hex(password: string, salt: string, iterations: number): Promise<string> {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = new Uint8Array(await crypto.subtle.deriveBits({
    name: "PBKDF2", hash: "SHA-256", salt: new TextEncoder().encode(salt), iterations,
  }, key, 256));
  return Array.from(bits, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function verifyPassword(password: string, salt: string, storedHash: string, iterations: number): Promise<boolean> {
  if (!password || !salt || !HASH_PATTERN.test(storedHash)) return false;
  const actual = hexBytes(await pbkdf2Hex(password, salt, iterations));
  const expected = hexBytes(storedHash);
  let mismatch = actual.length ^ expected.length;
  for (let index = 0; index < Math.max(actual.length, expected.length); index++) {
    mismatch |= (actual[index] || 0) ^ (expected[index] || 0);
  }
  return mismatch === 0;
}

function folderDocumentId(objectPath: string): Promise<string> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(objectPath))
    .then((digest) => Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""));
}

function validObjectPath(path: string): boolean {
  return path.startsWith("segmentation_gifs/") && path.split("/").length >= 2 &&
    path.split("/").length <= 5 && !path.split("/").some((part) => !part || part === "." || part === "..");
}

function legacyFolderSegmentationId(objectPath: string): string {
  return /^segmentation_gifs\/segmentation_([a-zA-Z0-9_-]{1,128})$/.exec(objectPath)?.[1] || "";
}

async function getOwnedLegacySegmentationIds(username: string): Promise<Set<string>> {
  const ids = new Set<string>();
  for (const collection of ["workstation_backups", "radiologist_workstation_backups"]) {
    const backup = await firestoreRequest(documentUrl(collection, username));
    if (!backup || stringField(backup, "owner_id") !== username) continue;
    for (const entry of backup.fields?.segmentation_results?.arrayValue?.values || []) {
      const id = entry?.mapValue?.fields?.id?.stringValue || "";
      if (/^[a-zA-Z0-9_-]{1,128}$/.test(id)) ids.add(id);
    }
  }
  return ids;
}

async function setFolderPassword(input: any) {
  const username = typeof input.username === "string" ? input.username.trim() : "";
  const accountPassword = typeof input.accountPassword === "string" ? input.accountPassword : "";
  const folderPassword = typeof input.folderPassword === "string" ? input.folderPassword : "";
  const objectPath = typeof input.objectPath === "string" ? input.objectPath : "";
  const isAccountFolder = objectPath.startsWith(`segmentation_gifs/${username}/segmentation_`);
  const legacySegmentationId = legacyFolderSegmentationId(objectPath);
  if (!username || !accountPassword || folderPassword.length < 6 || folderPassword.length > 256 ||
      !validObjectPath(objectPath) || (!isAccountFolder && !legacySegmentationId)) {
    fail(400, "invalid-argument", "Invalid folder-password details.");
  }
  if (!await readCloudAccount(username, accountPassword)) {
    fail(401, "unauthenticated", "Desktop account verification failed. Check the username and password synced from SQLite.");
  }
  if (!isAccountFolder && !(await getOwnedLegacySegmentationIds(username)).has(legacySegmentationId)) {
    fail(403, "permission-denied", "This legacy folder is not listed in your Firestore workstation backup, so NeuroQuant cannot change its password.");
  }
  const saltBytes = crypto.getRandomValues(new Uint8Array(16));
  const salt = Array.from(saltBytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  const passwordHash = await pbkdf2Hex(folderPassword, salt, 200_000);
  const docId = await folderDocumentId(objectPath);
  await firestoreRequest(documentUrl("segmentation_folder_access", docId), {
    method: "PATCH",
    body: JSON.stringify({ fields: {
      objectPath: { stringValue: objectPath },
      salt: { stringValue: salt },
      passwordHash: { stringValue: passwordHash },
      updatedAt: { integerValue: String(Date.now()) },
    } }),
  });
  return { ok: true };
}

async function getSegmentationFolderStatus(input: any) {
  const username = typeof input.username === "string" ? input.username.trim() : "";
  const accountPassword = typeof input.accountPassword === "string" ? input.accountPassword : "";
  if (!username || !accountPassword) fail(400, "invalid-argument", "Desktop account credentials are required to check folder status.");
  if (!await readCloudAccount(username, accountPassword)) {
    fail(401, "unauthenticated", "Desktop account verification failed. Check the username and password synced from SQLite.");
  }

  const folderPrefix = `segmentation_gifs/${username}/`;
  const ownedLegacyIds = await getOwnedLegacySegmentationIds(username);
  const folders = new Map<string, { objectPath: string; name: string; hasPassword: boolean }>();
  let pageToken = "";
  do {
    const url = new URL(documentUrl("segmentation_artifacts"));
    url.searchParams.set("pageSize", "1000");
    if (pageToken) url.searchParams.set("pageToken", pageToken);
    const page = await firestoreRequest(url.toString());
    if (!page) break;
    for (const document of page.documents || []) {
      const fields = document.fields || {};
      const objectPath = fields.supabase_object_path?.stringValue || "";
      const bucket = fields.supabase_bucket?.stringValue || "";
      const fileName = fields.file_name?.stringValue || objectPath.split("/").pop() || "";
      const parentPath = objectPath.slice(0, objectPath.lastIndexOf("/"));
      const legacyId = legacyFolderSegmentationId(parentPath);
      const isOwned = objectPath.startsWith(folderPrefix) || Boolean(legacyId && ownedLegacyIds.has(legacyId));
      if (bucket !== "neuro" || !isOwned || !validObjectPath(objectPath) || !/\.gif$/i.test(fileName)) continue;
      const folderPath = objectPath.slice(0, objectPath.lastIndexOf("/"));
      if (!folders.has(folderPath)) {
        const leaf = folderPath.split("/").pop() || folderPath;
        folders.set(folderPath, {
          objectPath: folderPath,
          name: fields.folder_name?.stringValue || leaf.replace(/^segmentation_/, ""),
          hasPassword: false,
        });
      }
    }
    pageToken = page.nextPageToken || "";
  } while (pageToken);

  for (const folder of folders.values()) {
    const docId = await folderDocumentId(folder.objectPath);
    const passwordDoc = await firestoreRequest(documentUrl("segmentation_folder_access", docId));
    folder.hasPassword = Boolean(stringField(passwordDoc, "passwordHash") && stringField(passwordDoc, "salt"));
  }
  return { folders: Array.from(folders.values()).sort((a, b) => a.name.localeCompare(b.name)) };
}

async function setPasswordsForFolders(input: any) {
  const username = typeof input.username === "string" ? input.username.trim() : "";
  const accountPassword = typeof input.accountPassword === "string" ? input.accountPassword : "";
  const folderPassword = typeof input.folderPassword === "string" ? input.folderPassword : "";
  const objectPaths = Array.isArray(input.objectPaths) ? [...new Set(input.objectPaths)] : [];
  if (!username || !accountPassword || folderPassword.length < 6 || folderPassword.length > 256 ||
      objectPaths.length > 500 || objectPaths.some((path) => typeof path !== "string")) {
    fail(400, "invalid-argument", "Invalid folder-password details.");
  }
  let updated = 0;
  for (const objectPath of objectPaths) {
    await setFolderPassword({ username, accountPassword, folderPassword, objectPath });
    updated++;
  }
  return { ok: true, updated };
}

async function createDesktopUploadUrl(input: any) {
  const username = typeof input.username === "string" ? input.username.trim() : "";
  const password = typeof input.password === "string" ? input.password : "";
  const segmentationId = String(input.segmentationId || "");
  const expectedFolderName = `segmentation_${segmentationId}`;
  const folderName = typeof input.folderName === "string" ? input.folderName : expectedFolderName;
  const fileName = typeof input.fileName === "string" ? input.fileName : "";
  if (!username || username.length > 128 || !password || password.length > 1024 ||
      !/^[a-zA-Z0-9_-]{1,128}$/.test(segmentationId) || folderName !== expectedFolderName ||
      !/^[a-zA-Z0-9_.-]{1,180}\.gif$/i.test(fileName)) {
    fail(400, "invalid-argument", "Invalid account or GIF details.");
  }
  if (!await readCloudAccount(username, password)) {
    fail(401, "unauthenticated", "The username or password is incorrect. Sync the desktop account from SQLite to Firestore first.");
  }
  const objectPath = `segmentation_gifs/${username}/${folderName}/${fileName}`;
  const { data, error } = await getSupabaseClient().storage.from("neuro").createSignedUploadUrl(objectPath, { upsert: true });
  if (error || !data?.signedUrl) {
    console.error("Could not create Supabase upload URL", error?.message);
    fail(502, "storage", "Could not prepare the GIF upload to the Supabase neuro bucket.");
  }
  const signedUrl = data.signedUrl.startsWith("http")
    ? data.signedUrl
    : `${Deno.env.get("SUPABASE_URL") || "https://rgvieraaviomiovcqyyu.supabase.co"}/storage/v1${data.signedUrl}`;
  return { signedUrl, objectPath, bucket: "neuro", name: fileName, expiresInSeconds: 120 };
}

async function listSegmentationFolders() {
  const cases = new Map<string, { name: string; path: string; files: any[]; hasPassword: boolean }>();
  let pageToken = "";
  do {
    const url = new URL(documentUrl("segmentation_artifacts"));
    url.searchParams.set("pageSize", "1000");
    if (pageToken) url.searchParams.set("pageToken", pageToken);
    const page = await firestoreRequest(url.toString());
    if (!page) break;
    for (const document of page.documents || []) {
      const fields = document.fields || {};
      const bucket = fields.supabase_bucket?.stringValue || "";
      const objectPath = fields.supabase_object_path?.stringValue || "";
      const fileName = fields.file_name?.stringValue || objectPath.split("/").pop() || "";
      if (bucket !== "neuro" || !validObjectPath(objectPath) || !/\.gif$/i.test(fileName)) continue;
      const folderPath = objectPath.slice(0, objectPath.lastIndexOf("/"));
      const folderLeaf = folderPath.split("/").pop() || folderPath;
      if (!cases.has(folderPath)) {
        cases.set(folderPath, {
          name: fields.folder_name?.stringValue || folderLeaf.replace(/^segmentation_/, ""),
          path: folderPath,
          files: [],
          hasPassword: false,
        });
      }
    }
    pageToken = page.nextPageToken || "";
  } while (pageToken);
  for (const folder of cases.values()) {
    const access = await firestoreRequest(documentUrl("segmentation_folder_access", await folderDocumentId(folder.path)));
    folder.hasPassword = Boolean(stringField(access, "passwordHash") && stringField(access, "salt"));
  }
  return { cases: Array.from(cases.values()).sort((a, b) => a.name.localeCompare(b.name)) };
}

async function unlockSegmentationFolder(input: any) {
  const objectPath = typeof input.objectPath === "string" ? input.objectPath : "";
  const password = typeof input.password === "string" ? input.password : "";
  if (!validObjectPath(objectPath) || !password) fail(400, "invalid-argument", "A folder password is required.");
  const docId = await folderDocumentId(objectPath);
  const access = await firestoreRequest(documentUrl("segmentation_folder_access", docId));
  if (!access) fail(403, "permission-denied", "No password has been saved for this folder from the desktop yet.");
  const salt = stringField(access, "salt");
  const storedHash = stringField(access, "passwordHash");
  if (!await verifyPassword(password, salt, storedHash, 200_000)) {
    fail(403, "permission-denied", "The folder password is incorrect.");
  }
  const storage = getSupabaseClient().storage;
  const { data: objects, error } = await storage.from("neuro").list(objectPath, { limit: 1000 });
  if (error) {
    console.error("Could not list GIF folder", error.message);
    fail(502, "storage", "The folder is in Firestore, but its GIF files could not be found in the Supabase neuro bucket.");
  }
  const files = [];
  for (const item of objects || []) {
    if (!item.id || !/\.gif$/i.test(item.name)) continue;
    const path = `${objectPath}/${item.name}`;
    const signed = await storage.from("neuro").createSignedUrl(path, 900);
    if (!signed.error && signed.data?.signedUrl) {
      files.push({ name: item.name, path, url: signed.data.signedUrl, size: Number(item.metadata?.size || 0) });
    }
  }
  if (!files.length) fail(404, "not-found", "No GIF files were found in this Supabase folder.");
  const leaf = objectPath.split("/").pop() || objectPath;
  return { case: { name: leaf.replace(/^segmentation_/, ""), path: objectPath, files } };
}

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (request.method !== "POST") return Response.json({ error: "Use POST." }, { status: 405, headers: corsHeaders });
  try {
    const body = await request.json();
    const action = String(body.action || "");
    let result;
    switch (action) {
      case "listSegmentationFolders": result = await listSegmentationFolders(); break;
      case "getSegmentationFolderStatus": result = await getSegmentationFolderStatus(body); break;
      case "unlockSegmentationFolder": result = await unlockSegmentationFolder(body); break;
      case "setSegmentationFolderPassword": result = await setFolderPassword(body); break;
      case "setSegmentationFolderPasswords": result = await setPasswordsForFolders(body); break;
      case "createDesktopUploadUrl": result = await createDesktopUploadUrl(body); break;
      default: fail(400, "invalid-argument", "Unknown folder service action.");
    }
    return Response.json(result, { headers: corsHeaders });
  } catch (error) {
    const status = Number((error as any)?.status) || 500;
    const code = String((error as any)?.code || "internal");
    const message = status >= 500 && !((error as any)?.status)
      ? "Folder service failed. Check its Supabase Edge Function logs and secrets."
      : String((error as Error)?.message || "Folder service failed.");
    if (status >= 500) console.error("NeuroQuant folder service error", error);
    return Response.json({ code, error: message }, { status, headers: corsHeaders });
  }
});
