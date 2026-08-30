// Shared layout, navigation, PIN-confirmed identity, and small helpers
// used by every page in the Asset Tracking Management System.

import { db } from "./firebase-config.js";
import { COLLECTIONS } from "./collections.js";
import {
  collection,
  query,
  where,
  getDocs,
  doc,
  getDoc,
  updateDoc,
  runTransaction,
  addDoc,
  serverTimestamp,
  increment,
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js";

/* ------------------------------------------------------------------ */
/* Tiny DOM helpers                                                    */
/* ------------------------------------------------------------------ */
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  (Array.isArray(children) ? children : [children]).forEach((c) => {
    if (c === null || c === undefined) return;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return node;
}

export function debounce(fn, delay = 250) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), delay);
  };
}

/* ------------------------------------------------------------------ */
/* Toasts                                                              */
/* ------------------------------------------------------------------ */
export function toast(message, type = "info") {
  let host = $("#toast-host");
  if (!host) {
    host = el("div", { id: "toast-host" });
    document.body.appendChild(host);
  }
  const node = el("div", { class: `toast toast-${type}` }, message);
  host.appendChild(node);
  requestAnimationFrame(() => node.classList.add("show"));
  setTimeout(() => {
    node.classList.remove("show");
    setTimeout(() => node.remove(), 300);
  }, 3500);
}

/* ------------------------------------------------------------------ */
/* Date helpers                                                        */
/* ------------------------------------------------------------------ */
export function todayISO() {
  return new Date().toISOString().slice(0, 10);
}
export function nowISO() {
  return new Date().toISOString();
}
export function formatDate(value) {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d)) return String(value);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}
export function formatDateTime(value) {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d)) return String(value);
  return (
    d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }) +
    " " +
    d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
  );
}
export function formatCurrency(value) {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  if (isNaN(n)) return String(value);
  return n.toLocaleString("en-GB", { style: "currency", currency: "GBP" });
}

/* ------------------------------------------------------------------ */
/* Sequential ID generator — matches the dictionary's Auto Number      */
/* fields (e.g. AST000123, EMP0012, SUP001) using a Firestore          */
/* transaction against Counters/{prefix} so numbers never collide.     */
/* ------------------------------------------------------------------ */
export async function nextSequenceNumber(counterName) {
  const counterRef = doc(db, COLLECTIONS.COUNTERS, counterName);
  return runTransaction(db, async (tx) => {
    const snap = await tx.get(counterRef);
    const current = snap.exists() ? snap.data().value || 0 : 0;
    const value = current + 1;
    tx.set(counterRef, { value }, { merge: true });
    return value;
  });
}

export async function nextId(prefix, pad = 4) {
  const next = await nextSequenceNumber(prefix);
  return `${prefix}${String(next).padStart(pad, "0")}`;
}

/**
 * Assets need a matching AssetID/QRCode pair generated from the same
 * underlying number (e.g. AST000123 / QR000123), per the dictionary's
 * example values. Uses a single "ASSET" counter shared by both.
 */
export async function nextAssetIdentifiers() {
  const n = await nextSequenceNumber("ASSET");
  const padded = String(n).padStart(6, "0");
  return { assetId: `AST${padded}`, qrCode: `QR${padded}` };
}

/* ------------------------------------------------------------------ */
/* Audit log — every create/update across the app writes one of these  */
/* so there is a single place to review "who did what, when".          */
/* ------------------------------------------------------------------ */
export async function logAudit({ recordType, recordId, action, description, employeeIds, assetId }) {
  const actor = getActingAs();
  // Most lifecycle actions already log with recordType:"Asset" and
  // recordId:<AssetID> — that recordId IS the asset. A few (Recharge
  // cases, keyed by their own RechargeID) need the caller to pass the
  // related assetId explicitly so the Audit Report's "view by Asset"
  // search can still find them.
  const resolvedAssetId = assetId || (recordType === "Asset" ? recordId : null);
  try {
    await addDoc(collection(db, COLLECTIONS.AUDIT_LOG), {
      recordType,
      recordId,
      action,
      description: description || "",
      performedBy: actor.id || null,
      performedByName: actor.name || "Unknown",
      assetId: resolvedAssetId || null,
      // Employee(s) this entry concerns (e.g. the from/to employee on an
      // allocation, transfer, or recharge) — lets the Audit Report look
      // an entry up by Employee as well as by Asset. Optional; entries
      // without it just won't surface under an Employee search.
      employeeIds: Array.isArray(employeeIds) ? employeeIds.filter(Boolean) : employeeIds ? [employeeIds] : [],
      timestamp: serverTimestamp(),
      timestampClient: nowISO(),
    });
  } catch (err) {
    // Audit logging must never block the primary action.
    console.warn("Audit log write failed:", err);
  }
}

/* ------------------------------------------------------------------ */
/* Current actor — resolved by PIN confirmation (see requirePin below) */
/* This is deliberately in-memory only, NOT persisted to localStorage: */
/* every data-changing action must re-confirm a PIN, so a page reload  */
/* (or a different person walking up to the same screen) always has   */
/* to enter one again rather than silently inheriting the last person */
/* who typed one in.                                                   */
/* ------------------------------------------------------------------ */
let currentActor = { id: null, name: null };

export function getActingAs() {
  return currentActor;
}

function setCurrentActor(id, name) {
  currentActor = { id, name };
}

/* ------------------------------------------------------------------ */
/* PIN confirmation — the Users register (see users.html) holds a      */
/* Name + 4-digit PIN for everyone who operates the system. Every      */
/* action that changes data asks for that PIN, resolves it to a name,  */
/* and uses that name as the actor for the on-screen result and the    */
/* audit trail entry logAudit() writes right after.                    */
/* ------------------------------------------------------------------ */
let usersCache = null;

/** Call after adding/editing/deactivating a user so the next requirePin() re-reads the register. */
export function invalidateUsersCache() {
  usersCache = null;
}

async function loadActiveUsersForPin() {
  if (usersCache) return usersCache;
  const snap = await getDocs(collection(db, COLLECTIONS.USERS));
  usersCache = snap.docs
    .map((d) => ({ id: d.id, ...d.data() }))
    .filter((u) => u.Active !== false);
  return usersCache;
}

/**
 * Prompts for a 4-digit PIN and resolves it against the Users register.
 * Resolves to { id, name } on a correct PIN, or null if the person
 * cancels. On success it also updates the module-level actor (see
 * getActingAs above), so the very next logAudit()/recordMovement() call,
 * or any CreatedBy/CompletedBy field a handler stamps itself right
 * after awaiting this, picks up the confirmed name automatically.
 */
export async function requirePin(actionLabel = "") {
  const users = await loadActiveUsersForPin();
  return new Promise((resolve) => {
    let settled = false;
    const input = el("input", {
      type: "password",
      inputmode: "numeric",
      pattern: "[0-9]*",
      maxlength: "4",
      autocomplete: "off",
      style: "font-size:26px; letter-spacing:14px; text-align:center; max-width:170px;",
    });
    const errorMsg = el("div", { class: "field-error", style: "min-height:16px; margin-top:6px;" });
    const cancelBtn = el("button", { type: "button", class: "btn btn-secondary" }, "Cancel");
    const confirmBtn = el("button", { type: "button", class: "btn btn-primary" }, "Confirm");

    const body = el("div", {}, [
      el(
        "p",
        { class: "muted", style: "margin-bottom:14px;" },
        actionLabel ? `Enter your 4-digit PIN to confirm: ${actionLabel}` : "Enter your 4-digit PIN to confirm this action."
      ),
      el("div", { class: "field" }, [el("label", {}, "PIN"), input]),
      errorMsg,
      el("div", { class: "form-actions" }, [cancelBtn, confirmBtn]),
    ]);

    function finish(result) {
      if (settled) return;
      settled = true;
      obs.disconnect();
      closeModal();
      resolve(result);
    }

    function attempt() {
      const val = (input.value || "").trim();
      if (!/^\d{4}$/.test(val)) {
        errorMsg.textContent = "Enter a 4-digit PIN.";
        return;
      }
      if (users.length === 0) {
        errorMsg.textContent = "No users set up yet — add one from the Users page first.";
        return;
      }
      const match = users.find((u) => String(u.Pin || "").trim() === val);
      if (!match) {
        errorMsg.textContent = "PIN not recognized.";
        input.value = "";
        input.focus();
        return;
      }
      setCurrentActor(match.id, match.Name || "Unknown");
      finish({ id: match.id, name: match.Name || "Unknown" });
    }

    openModal("Confirm PIN", body);
    confirmBtn.addEventListener("click", attempt);
    cancelBtn.addEventListener("click", () => finish(null));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        attempt();
      }
    });

    // Closing the modal via the × button or a backdrop click (both wired
    // to the shared closeModal(), not to our resolve) must still settle
    // this promise as a cancel, so the caller's handler doesn't hang.
    const obs = new MutationObserver(() => {
      if (!document.body.contains(overlayEl)) finish(null);
    });
    const overlayEl = $("#modal-overlay");
    if (overlayEl) obs.observe(document.body, { childList: true });

    setTimeout(() => input.focus(), 30);
  });
}

/* ------------------------------------------------------------------ */
/* Allocation / Transfer / Return shared helpers                       */
/* ------------------------------------------------------------------ */

/** Human-readable reason an asset can't be allocated/transferred right now. */
export const INELIGIBLE_STATUS_REASONS = {
  Assigned: "Already allocated to another employee",
  "Under Repair": "Asset is under repair",
  "Awaiting Inspection": "Asset is awaiting inspection",
  "Away for Calibration": "Asset is away for calibration",
  Retired: "Asset has been retired",
  Missing: "Asset is reported missing",
  Stolen: "Asset is reported lost or stolen",
  "Lost in Transit": "Asset is reported lost in transit",
};

/**
 * The single "open" Assignment History record for an asset — the one
 * allocation/transfer that hasn't been closed with an AssignmentEndDate
 * yet. Returns null if the asset currently has no holder on record.
 */
export async function findActiveAssignment(assetId) {
  const q = query(collection(db, COLLECTIONS.ASSIGNMENT_HISTORY), where("AssetID", "==", assetId));
  const snap = await getDocs(q);
  const open = snap.docs
    .map((d) => ({ id: d.id, ...d.data() }))
    .filter((r) => !r.AssignmentEndDate)
    .sort((a, b) => new Date(b.AssignmentStartDate || 0) - new Date(a.AssignmentStartDate || 0));
  return open[0] || null;
}

/**
 * Best-effort link from an employee to their "location" in the Location
 * Register (a location with ReferenceSource = Employee, ReferenceRecord =
 * that employee). Not every employee will have one set up yet — callers
 * should treat a null return as "unknown location", not an error.
 */
export async function findEmployeeLocation(employeeId) {
  if (!employeeId) return null;
  const q = query(
    collection(db, COLLECTIONS.LOCATION_REGISTER),
    where("ReferenceSource", "==", "Employee"),
    where("ReferenceRecord", "==", employeeId)
  );
  try {
    const snap = await getDocs(q);
    return snap.empty ? null : snap.docs[0].id;
  } catch {
    return null;
  }
}

export async function adjustEmployeeAssetCount(employeeId, delta) {
  if (!employeeId) return;
  try {
    await updateDoc(doc(db, COLLECTIONS.EMPLOYEE_REGISTER, employeeId), {
      CurrentAssetCount: increment(delta),
    });
  } catch (err) {
    console.warn("Could not adjust employee asset count:", err);
  }
}

export async function recordMovement({ assetId, action, fromLocation, toLocation, notes }) {
  const actor = getActingAs();
  await addDoc(collection(db, COLLECTIONS.MOVEMENT_REGISTER), {
    AssetID: assetId,
    Action: action,
    FromLocation: fromLocation || null,
    ToLocation: toLocation || null,
    MovementDateTime: nowISO(),
    CompletedBy: actor.name || "Unknown",
    Notes: notes || "",
    CreatedDate: nowISO(),
    CreatedBy: actor.name || "Unknown",
  });
}

/* ------------------------------------------------------------------ */
/* Calibration / Repair shared helpers                                 */
/* ------------------------------------------------------------------ */

/** RepairStatus / CalibrationStatus values that mean "this record is done". */
function isOpenStatus(status) {
  return !!status && !status.startsWith("Completed") && status !== "Retired" && status !== "Beyond Repair - Retired";
}

/**
 * The open (not-yet-completed) CalibrationRegister record for an asset,
 * if one exists — i.e. the asset is currently "Away for Calibration".
 */
export async function findOpenCalibration(assetId) {
  const q = query(collection(db, COLLECTIONS.CALIBRATION_REGISTER), where("AssetID", "==", assetId));
  const snap = await getDocs(q);
  const open = snap.docs
    .map((d) => ({ id: d.id, ...d.data() }))
    .filter((r) => isOpenStatus(r.CalibrationStatus))
    .sort((a, b) => new Date(b.CalibrationStartDate || 0) - new Date(a.CalibrationStartDate || 0));
  return open[0] || null;
}

/**
 * The open (not-yet-completed) RepairRegister record for an asset, if
 * one exists — i.e. the asset is currently "Under Repair". Created
 * automatically by the Return flow, the Calibration "Repair Required" /
 * "Beyond Repair" outcomes, or manually from the Repair queue.
 */
export async function findOpenRepair(assetId) {
  const q = query(collection(db, COLLECTIONS.REPAIR_REGISTER), where("AssetID", "==", assetId));
  const snap = await getDocs(q);
  const open = snap.docs
    .map((d) => ({ id: d.id, ...d.data() }))
    .filter((r) => isOpenStatus(r.RepairStatus))
    .sort((a, b) => new Date(b.CreatedDate || 0) - new Date(a.CreatedDate || 0));
  return open[0] || null;
}

/** All open (not-yet-completed) RepairRegister records, for the Repair queue page. */
export async function findAllOpenRepairs() {
  const all = await fetchAll(COLLECTIONS.REPAIR_REGISTER);
  return all
    .filter((r) => isOpenStatus(r.RepairStatus))
    .sort((a, b) => new Date(a.CreatedDate || 0) - new Date(b.CreatedDate || 0));
}

/**
 * Adds a calibration interval (Days/Months/Years) to a date, matching
 * Asset Types' CalibrationInterval + CalibrationUnit fields.
 */
export function calculateNextCalibrationDue(fromDateISO, interval, unit) {
  const d = new Date(fromDateISO);
  const n = Number(interval) || 0;
  if (unit === "Days") d.setDate(d.getDate() + n);
  else if (unit === "Years") d.setFullYear(d.getFullYear() + n);
  else d.setMonth(d.getMonth() + n); // default: Months
  return d.toISOString().slice(0, 10);
}

/* ------------------------------------------------------------------ */
/* Incident / Recovery / Recharge / Retirement shared helpers          */
/* ------------------------------------------------------------------ */

/** IncidentStatus values that mean "this incident is resolved/closed". */
export function isOpenIncident(status) {
  return !!status && status !== "Closed";
}

/** The open (unresolved) AssetIncident record for an asset, if one exists. */
export async function findOpenIncident(assetId) {
  const q = query(collection(db, COLLECTIONS.ASSET_INCIDENT), where("AssetID", "==", assetId));
  const snap = await getDocs(q);
  const open = snap.docs
    .map((d) => ({ id: d.id, ...d.data() }))
    .filter((r) => isOpenIncident(r.IncidentStatus))
    .sort((a, b) => new Date(b.CreatedDate || 0) - new Date(a.CreatedDate || 0));
  return open[0] || null;
}

/** All open AssetIncident records, for the Incidents queue page. */
export async function findAllOpenIncidents() {
  const all = await fetchAll(COLLECTIONS.ASSET_INCIDENT);
  return all.filter((r) => isOpenIncident(r.IncidentStatus)).sort((a, b) => new Date(a.CreatedDate || 0) - new Date(b.CreatedDate || 0));
}

/** RecoveryStatus values that mean "this recovery case is resolved/closed". */
export function isOpenRecovery(status) {
  return !!status && !["Recovered", "Closed", "Cannot Be Recovered"].includes(status);
}

/** The open AssetRecovery record for an asset, if one exists. */
export async function findOpenRecovery(assetId) {
  const q = query(collection(db, COLLECTIONS.ASSET_RECOVERY), where("AssetID", "==", assetId));
  const snap = await getDocs(q);
  const open = snap.docs
    .map((d) => ({ id: d.id, ...d.data() }))
    .filter((r) => isOpenRecovery(r.RecoveryStatus))
    .sort((a, b) => new Date(b.CreatedDate || 0) - new Date(a.CreatedDate || 0));
  return open[0] || null;
}

/** All open AssetRecovery records, for the Recovery queue page. */
export async function findAllOpenRecoveries() {
  const all = await fetchAll(COLLECTIONS.ASSET_RECOVERY);
  return all.filter((r) => isOpenRecovery(r.RecoveryStatus)).sort((a, b) => new Date(a.CreatedDate || 0) - new Date(b.CreatedDate || 0));
}

/** RechargeStatus values that mean "this recharge case is resolved/closed". */
export function isOpenRecharge(status) {
  return !!status && !["Closed", "Complete"].includes(status);
}

/** All open RechargeRegister records, for the Recharge queue page. */
export async function findAllOpenRecharges() {
  const all = await fetchAll(COLLECTIONS.RECHARGE_REGISTER);
  return all.filter((r) => isOpenRecharge(r.RechargeStatus)).sort((a, b) => new Date(a.CreatedDate || 0) - new Date(b.CreatedDate || 0));
}

/** The open RechargeRegister record for an asset, if one exists. */
export async function findOpenRecharge(assetId) {
  const all = await fetchAll(COLLECTIONS.RECHARGE_REGISTER);
  const open = all
    .filter((r) => r.AssetID === assetId && isOpenRecharge(r.RechargeStatus))
    .sort((a, b) => new Date(b.CreatedDate || 0) - new Date(a.CreatedDate || 0));
  return open[0] || null;
}

/** RetirementStatus values that mean "this retirement case is finished". */
export function isOpenRetirement(status) {
  return !!status && !["Retirement Complete", "Declined"].includes(status);
}

/** The open RetirementRegister record for an asset, if one exists — i.e. a
 * retirement that's mid-review or awaiting physical disposal (includes the
 * stub records created automatically by the Repair "Beyond Repair" path). */
export async function findOpenRetirement(assetId) {
  const q = query(collection(db, COLLECTIONS.RETIREMENT_REGISTER), where("AssetID", "==", assetId));
  const snap = await getDocs(q);
  const open = snap.docs
    .map((d) => ({ id: d.id, ...d.data() }))
    .filter((r) => isOpenRetirement(r.RetirementStatus))
    .sort((a, b) => new Date(b.CreatedDate || 0) - new Date(a.CreatedDate || 0));
  return open[0] || null;
}

/** All open RetirementRegister records, for the Retirement queue page. */
export async function findAllOpenRetirements() {
  const all = await fetchAll(COLLECTIONS.RETIREMENT_REGISTER);
  return all.filter((r) => isOpenRetirement(r.RetirementStatus)).sort((a, b) => new Date(a.CreatedDate || 0) - new Date(b.CreatedDate || 0));
}

/**
 * Per the flow chart's Retirement Rules: "The system must validate whether
 * any outstanding actions exist before retirement is completed... all
 * outstanding actions must be resolved before the status can be changed to
 * Retired." Checks the workflows this app tracks and returns a plain-language
 * list — empty means clear to retire.
 */
export async function checkOutstandingActions(assetId) {
  const actions = [];
  const [assignment, repair, calibration, recharge, incident, recovery] = await Promise.all([
    findActiveAssignment(assetId),
    findOpenRepair(assetId),
    findOpenCalibration(assetId),
    findOpenRecharge(assetId),
    findOpenIncident(assetId),
    findOpenRecovery(assetId),
  ]);
  if (assignment) actions.push("Active employee allocation — asset is still assigned to an employee");
  if (repair) actions.push(`Open repair job (${repair.id})`);
  if (calibration) actions.push(`Open calibration job (${calibration.id})`);
  if (recharge) actions.push(`Open recharge case (${recharge.id})`);
  if (incident) actions.push(`Open incident (${incident.id})`);
  if (recovery) actions.push(`Open recovery case (${recovery.id})`);
  return actions;
}

/**
 * Builds the hidden physical-label version of an asset's QR code — sized
 * to fit a 25.4mm × 12.6mm label (the smallest common multi-purpose label
 * size) with the QR square on the left and the Asset ID as small text on
 * the right. This is deliberately separate from whatever larger on-screen
 * QR preview a page shows — this node is positioned off-screen via CSS
 * (see .qr-print-label in styles.css) so it never appears in the normal
 * layout, and only becomes visible when a caller gives it the id
 * "qr-print-area" and calls window.print() (see the print-isolation rule
 * in styles.css, which also sets @page to this exact label size).
 * Callers are responsible for assigning/clearing that id themselves —
 * this only builds the node.
 */
export function buildQrLabel({ qrCode, assetId }) {
  const codeHost = el("div", { class: "qr-print-label-code" });
  const label = el("div", { class: "qr-print-label" }, [
    codeHost,
    el("div", { class: "qr-print-label-text" }, assetId || ""),
  ]);
  if (qrCode && window.QRCode) {
    new QRCode(codeHost, { text: qrCode, width: 200, height: 200 });
  }
  return label;
}

/**
 * A minimal signature pad: draws onto a canvas with mouse/touch/pen,
 * and can hand back either a data URL or a Blob for upload. No external
 * library needed — this is the whole thing.
 */
export function createSignaturePad(host, { width = 400, height = 140 } = {}) {
  host.innerHTML = "";
  const canvas = el("canvas", {
    width: String(width),
    height: String(height),
    style: `border:1px solid var(--slate-300); border-radius:6px; background:#fff; touch-action:none; cursor:crosshair; width:100%; max-width:${width}px; height:auto;`,
  });
  const clearBtn = el("button", { type: "button", class: "btn btn-ghost btn-sm" }, "Clear Signature");
  const wrap = el("div", { style: "display:flex; flex-direction:column; gap:8px; align-items:flex-start;" }, [canvas, clearBtn]);
  host.appendChild(wrap);

  const ctx = canvas.getContext("2d");
  ctx.lineWidth = 2;
  ctx.lineCap = "round";
  ctx.strokeStyle = "#1e293b";
  let drawing = false;
  let hasDrawn = false;

  function pos(e) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    const point = e.touches ? e.touches[0] : e;
    return { x: (point.clientX - rect.left) * scaleX, y: (point.clientY - rect.top) * scaleY };
  }
  function start(e) {
    drawing = true;
    hasDrawn = true;
    const p = pos(e);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    e.preventDefault();
  }
  function move(e) {
    if (!drawing) return;
    const p = pos(e);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
    e.preventDefault();
  }
  function end() {
    drawing = false;
  }
  canvas.addEventListener("mousedown", start);
  canvas.addEventListener("mousemove", move);
  window.addEventListener("mouseup", end);
  canvas.addEventListener("touchstart", start);
  canvas.addEventListener("touchmove", move);
  canvas.addEventListener("touchend", end);
  clearBtn.addEventListener("click", () => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    hasDrawn = false;
  });

  return {
    isEmpty: () => !hasDrawn,
    clear: () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      hasDrawn = false;
    },
    getBlob: () => new Promise((resolve) => canvas.toBlob(resolve, "image/png")),
  };
}

/* ------------------------------------------------------------------ */
/* Navigation                                                          */
/* ------------------------------------------------------------------ */
const NAV_SECTIONS = [
  {
    heading: null,
    items: [{ key: "dashboard", label: "Dashboard", href: "dashboard.html", icon: "grid" }],
  },
  {
    heading: "Assets",
    items: [
      { key: "asset-register", label: "Asset Register", href: "asset-register.html", icon: "list" },
      { key: "create-asset", label: "Create / Duplicate Asset", href: "create-asset.html", icon: "plus-square" },
      { key: "asset-types", label: "Asset Types", href: "asset-types.html", icon: "tag" },
      { key: "asset-categories", label: "Asset Categories", href: "asset-categories.html", icon: "folder" },
    ],
  },
  {
    heading: "People & Places",
    items: [
      { key: "employees", label: "Employee Register", href: "employees.html", icon: "user" },
      { key: "suppliers", label: "Supplier Register", href: "suppliers.html", icon: "truck" },
      { key: "locations", label: "Location Register", href: "locations.html", icon: "map-pin" },
      { key: "location-types", label: "Location Types", href: "location-types.html", icon: "layers" },
    ],
  },
  {
    heading: "Asset Processes",
    items: [
      { key: "allocate-asset", label: "Allocate Asset", href: "allocate-asset.html", icon: "log-out" },
      { key: "transfer-asset", label: "Transfer Asset", href: "transfer-asset.html", icon: "shuffle" },
      { key: "return-asset", label: "Receive Returned Asset", href: "return-asset.html", icon: "log-in" },
      { key: "calibration", label: "Calibration", href: "calibration.html", icon: "gauge" },
      { key: "repair", label: "Repair", href: "repair.html", icon: "wrench" },
      { key: "incidents", label: "Report Damage / Loss", href: "incident.html", icon: "alert-triangle" },
      { key: "recovery", label: "Asset Recovery", href: "recovery.html", icon: "life-buoy" },
      { key: "recharge", label: "Recharge", href: "recharge.html", icon: "battery" },
      { key: "retirement", label: "Retirement", href: "retirement.html", icon: "archive" },
    ],
  },
  {
    heading: "Administration",
    items: [
      { key: "users", label: "Users (Name & PIN)", href: "users.html", icon: "key" },
      { key: "audit-report", label: "Audit Report", href: "audit-report.html", icon: "clipboard" },
    ],
  },
];

/* ------------------------------------------------------------------ */
/* Sidebar icon set — small self-contained line icons (no icon font   */
/* / external dependency needed).                                     */
/* ------------------------------------------------------------------ */
const ICON_PATHS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  list: '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
  "plus-square": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>',
  tag: '<path d="M3 7a2 2 0 0 1 2-2h7l9 9-9 9-9-9V7z"/><circle cx="8" cy="9" r="1.4"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>',
  user: '<circle cx="12" cy="8" r="3.5"/><path d="M4.5 20c0-4 3.4-6.5 7.5-6.5s7.5 2.5 7.5 6.5"/>',
  truck: '<rect x="2" y="8" width="12" height="8" rx="1"/><path d="M14 11h3.5L20 14v2h-2"/><circle cx="7" cy="18" r="1.6"/><circle cx="17" cy="18" r="1.6"/>',
  "map-pin": '<path d="M12 21s7-6.5 7-11a7 7 0 1 0-14 0c0 4.5 7 11 7 11z"/><circle cx="12" cy="10" r="2.3"/>',
  layers: '<path d="M12 3 21 8 12 13 3 8 12 3z"/><path d="M3 14l9 5 9-5"/>',
  "log-out": '<rect x="3" y="3" width="8" height="8" rx="1"/><path d="M13 12h8m0 0-3-3m3 3-3 3"/>',
  shuffle: '<path d="M3 7h9m0 0-3-3m3 3-3 3"/><path d="M21 17h-9m0 0 3-3m-3 3 3 3"/>',
  "log-in": '<rect x="13" y="13" width="8" height="8" rx="1"/><path d="M11 12H3m0 0 3-3m-3 3 3 3"/>',
  gauge: '<circle cx="12" cy="13" r="8"/><path d="M12 13 16 9"/><circle cx="12" cy="13" r="1.2"/>',
  wrench: '<path d="M14.7 6.3a4 4 0 1 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2 2-2-2 2-2z"/>',
  "alert-triangle": '<path d="M12 3 2 20h20L12 3z"/><line x1="12" y1="10" x2="12" y2="15"/><circle cx="12" cy="17.5" r="0.6" fill="currentColor" stroke="none"/>',
  "life-buoy": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><line x1="6.3" y1="6.3" x2="9.5" y2="9.5"/><line x1="14.5" y1="14.5" x2="17.7" y2="17.7"/><line x1="17.7" y1="6.3" x2="14.5" y2="9.5"/><line x1="9.5" y1="14.5" x2="6.3" y2="17.7"/>',
  battery: '<rect x="2" y="7" width="16" height="10" rx="2"/><line x1="21" y1="10" x2="21" y2="14"/><path d="M11 9l-3 5h3l-1 4 4-5h-3l1-4z" fill="currentColor" stroke="none"/>',
  archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><line x1="10" y1="13" x2="14" y2="13"/>',
  key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="M11 12 20.5 2.5"/><path d="M16.5 7 19 9.5"/><path d="M14 9.5 16 11.5"/>',
  clipboard: '<rect x="5" y="4" width="14" height="18" rx="2"/><rect x="9" y="2" width="6" height="4" rx="1"/><line x1="8" y1="11" x2="16" y2="11"/><line x1="8" y1="15" x2="16" y2="15"/><line x1="8" y1="19" x2="13" y2="19"/>',
};

function navIcon(name) {
  return el("span", {
    class: "nav-icon",
    html: `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICON_PATHS[name] || ""}</svg>`,
  });
}

function renderSidebar(activeKey) {
  const nav = el("nav", { class: "sidebar", id: "sidebar" });
  nav.appendChild(el("div", { class: "brand" }, [
    el("span", { class: "brand-mark" }, "AT"),
    el("div", { class: "brand-text" }, [
      el("span", { class: "brand-name" }, "ASSET TRACKING"),
      el("span", { class: "brand-sub" }, "Equipment Register"),
    ]),
  ]));

  NAV_SECTIONS.forEach((section) => {
    if (section.heading) nav.appendChild(el("div", { class: "nav-heading" }, section.heading));
    const list = el("ul", { class: "nav-list" });
    section.items.forEach((item) => {
      const li = el("li", {});
      const a = el(
        "a",
        {
          href: item.href,
          class: "nav-link" + (item.key === activeKey ? " active" : "") + (item.soon ? " disabled" : ""),
        },
        [navIcon(item.icon), el("span", { class: "nav-label" }, item.label), item.soon ? el("span", { class: "soon-tag" }, "soon") : null]
      );
      if (item.soon) {
        a.addEventListener("click", (e) => {
          e.preventDefault();
          toast(`${item.label} is planned for the next build phase.`, "info");
        });
      }
      li.appendChild(a);
      list.appendChild(li);
    });
    nav.appendChild(list);
  });

  return nav;
}

function renderTopbar(pageTitle) {
  const header = el("header", { class: "topbar", id: "topbar" });
  header.appendChild(el("h1", { class: "page-title" }, pageTitle));
  const right = el("div", { class: "topbar-right" });
  right.appendChild(
    el("span", { class: "muted", style: "font-size:12px;" }, "Actions are confirmed by PIN")
  );
  header.appendChild(right);
  return header;
}

/**
 * Builds the standard shell (sidebar + topbar) around whatever markup
 * already exists in #page-content. Call this once per page on load.
 */
export function initLayout(activeKey, pageTitle) {
  document.body.classList.add("app-shell");
  document.body.insertBefore(renderSidebar(activeKey), document.body.firstChild);
  const content = $("#page-content");
  document.body.insertBefore(renderTopbar(pageTitle), content);
}

/* ------------------------------------------------------------------ */
/* Modal dialog                                                        */
/* ------------------------------------------------------------------ */
export function openModal(title, bodyNode, { wide = false } = {}) {
  closeModal();
  const overlay = el("div", { class: "modal-overlay", id: "modal-overlay" });
  const panel = el("div", { class: "modal-panel" + (wide ? " wide" : "") });
  const header = el("div", { class: "modal-header" }, [
    el("h2", {}, title),
    el("button", { class: "modal-close", type: "button", onclick: closeModal }, "×"),
  ]);
  panel.appendChild(header);
  const body = el("div", { class: "modal-body" });
  body.appendChild(bodyNode);
  panel.appendChild(body);
  overlay.appendChild(panel);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });
  document.body.appendChild(overlay);
  return panel;
}

export function closeModal() {
  const existing = $("#modal-overlay");
  if (existing) existing.remove();
}

/* ------------------------------------------------------------------ */
/* Generic lookups used across several pages                           */
/* ------------------------------------------------------------------ */
export async function fetchAll(collectionName, { activeOnly = false } = {}) {
  const snap = await getDocs(collection(db, collectionName));
  let rows = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
  if (activeOnly) rows = rows.filter((r) => r.Active !== false);
  return rows;
}

export async function fetchOne(collectionName, id) {
  const snap = await getDoc(doc(db, collectionName, id));
  return snap.exists() ? { id: snap.id, ...snap.data() } : null;
}
