/**
 * Wolverine Grader Pro — License Server (Firebase Cloud Functions)
 *
 * Endpoints:
 *   POST /api/claim-purchase   — Claim a TPT purchase, generate license key
 *   POST /api/activate         — Activate a device with a license key
 *   POST /api/validate         — Validate token + check for updates
 *   POST /api/deactivate       — Deactivate a device
 *   POST /api/admin/licenses   — List licenses (admin only)
 *   POST /api/admin/reset      — Reset a license's activations (admin only)
 *   POST /api/admin/revoke     — Revoke a license (admin only)
 *   POST /api/admin/release    — Publish an app update (admin only)
 */

const functions = require("firebase-functions");
const admin = require("firebase-admin");
const express = require("express");
const cors = require("cors");
const jwt = require("jsonwebtoken");
const { v4: uuidv4 } = require("uuid");

admin.initializeApp();
const db = admin.firestore();

// ── Config ────────────────────────────────────────────────────────────────────
// Set these with: firebase functions:config:set license.jwt_secret="YOUR_SECRET" license.admin_key="YOUR_ADMIN_KEY"
// Or use environment variables in Firebase Gen2
const JWT_SECRET = process.env.JWT_SECRET || functions.config().license?.jwt_secret || "CHANGE_ME_IN_PRODUCTION";
const ADMIN_KEY = process.env.ADMIN_KEY || functions.config().license?.admin_key || "CHANGE_ME_ADMIN_KEY";
const TOKEN_EXPIRY_DAYS = 7; // Offline grace period
const DEFAULT_MAX_DEVICES = 2;
const PRODUCT_ID = "wolverine-grader-pro";

// ── Express App ───────────────────────────────────────────────────────────────
const app = express();
app.use(cors({ origin: true }));
app.use(express.json());

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Generate a license key like WGP-8K4D-2Q7M-X9P1 */
function generateLicenseKey() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // no I/O/0/1 confusion
  const seg = () => Array.from({ length: 4 }, () => chars[Math.floor(Math.random() * chars.length)]).join("");
  return `WGP-${seg()}-${seg()}-${seg()}`;
}

/** Generate a signed JWT token for the device */
function signToken(licenseKey, deviceId, licensedTo, maxDevices) {
  return jwt.sign(
    {
      licenseKey,
      deviceId,
      productId: PRODUCT_ID,
      licensedTo,
      maxDevices,
    },
    JWT_SECRET,
    { expiresIn: `${TOKEN_EXPIRY_DAYS}d` }
  );
}

/** Verify admin key from request header */
function requireAdmin(req, res, next) {
  const key = req.headers["x-admin-key"];
  if (!key || key !== ADMIN_KEY) {
    return res.status(403).json({ error: "Unauthorized" });
  }
  next();
}

/** Get the latest app release info */
async function getLatestRelease() {
  const snap = await db.collection("app_releases")
    .orderBy("releasedAt", "desc")
    .limit(1)
    .get();
  if (snap.empty) return null;
  return snap.docs[0].data();
}

// ── POST /api/claim-purchase ──────────────────────────────────────────────────
app.post("/api/claim-purchase", async (req, res) => {
  try {
    const { orderNumber, buyerName, buyerEmail, productId } = req.body;

    if (!orderNumber || !buyerName) {
      return res.status(400).json({ error: "Order number and buyer name are required." });
    }

    const cleanOrder = String(orderNumber).trim();
    const cleanName = String(buyerName).trim();
    const cleanEmail = buyerEmail ? String(buyerEmail).trim().toLowerCase() : "";

    // Check if this order was already claimed
    const existingSnap = await db.collection("orders")
      .where("orderNumber", "==", cleanOrder)
      .where("status", "==", "approved")
      .limit(1)
      .get();

    if (!existingSnap.empty) {
      // Return the existing license key so they can re-activate
      const existingOrder = existingSnap.docs[0].data();
      const licenseSnap = await db.collection("licenses")
        .where("licenseKey", "==", existingOrder.licenseKey)
        .limit(1)
        .get();

      if (!licenseSnap.empty) {
        return res.json({
          success: true,
          licenseKey: existingOrder.licenseKey,
          licensedTo: licenseSnap.docs[0].data().buyerName,
          alreadyClaimed: true,
          message: "This order was already claimed. Use the license key to activate your device.",
        });
      }
    }

    // Rate limiting: max 5 claims per order number pattern per hour
    // (simple protection — not Fort Knox)
    const oneHourAgo = new Date(Date.now() - 60 * 60 * 1000);
    const recentClaims = await db.collection("orders")
      .where("claimedAt", ">", oneHourAgo.toISOString())
      .limit(10)
      .get();

    if (recentClaims.size >= 10) {
      return res.status(429).json({ error: "Too many claims. Please try again later." });
    }

    // Generate license
    const licenseKey = generateLicenseKey();
    const licenseId = `LIC_${uuidv4().split("-")[0]}`;
    const now = new Date().toISOString();

    // Create order record
    await db.collection("orders").add({
      source: "tpt",
      orderNumber: cleanOrder,
      buyerEmail: cleanEmail,
      buyerName: cleanName,
      productId: productId || PRODUCT_ID,
      claimedAt: now,
      status: "approved",
      licenseKey,
      licenseId,
    });

    // Create license record
    await db.collection("licenses").doc(licenseId).set({
      licenseKey,
      licenseId,
      productId: PRODUCT_ID,
      buyerEmail: cleanEmail,
      buyerName: cleanName,
      source: "tpt",
      status: "active",
      maxDevices: DEFAULT_MAX_DEVICES,
      activationCount: 0,
      createdAt: now,
      lastActivatedAt: null,
    });

    return res.json({
      success: true,
      licenseKey,
      licenseId,
      licensedTo: cleanName,
      maxDevices: DEFAULT_MAX_DEVICES,
    });
  } catch (err) {
    console.error("claim-purchase error:", err);
    return res.status(500).json({ error: "Server error. Please try again." });
  }
});

// ── POST /api/activate ────────────────────────────────────────────────────────
app.post("/api/activate", async (req, res) => {
  try {
    const { licenseKey, deviceId, deviceName } = req.body;

    if (!licenseKey || !deviceId) {
      return res.status(400).json({ error: "License key and device ID are required." });
    }

    const cleanKey = String(licenseKey).trim().toUpperCase();

    // Find the license
    const licSnap = await db.collection("licenses")
      .where("licenseKey", "==", cleanKey)
      .limit(1)
      .get();

    if (licSnap.empty) {
      return res.status(404).json({ error: "License key not found. Please check and try again." });
    }

    const licDoc = licSnap.docs[0];
    const license = licDoc.data();

    if (license.status !== "active") {
      return res.status(403).json({ error: `License is ${license.status}. Contact support.` });
    }

    // Check if this device is already activated
    const existingAct = await db.collection("activations")
      .where("licenseId", "==", license.licenseId)
      .where("deviceId", "==", deviceId)
      .where("status", "==", "active")
      .limit(1)
      .get();

    const now = new Date().toISOString();

    if (!existingAct.empty) {
      // Re-activate (refresh token)
      const actDoc = existingAct.docs[0];
      await actDoc.ref.update({ lastSeenAt: now });
      const token = signToken(cleanKey, deviceId, license.buyerName, license.maxDevices);

      // Check for updates
      const latestRelease = await getLatestRelease();

      return res.json({
        success: true,
        token,
        licensedTo: license.buyerName,
        maxDevices: license.maxDevices,
        activationCount: license.activationCount,
        reactivated: true,
        latestVersion: latestRelease?.version || null,
        downloadUrl: latestRelease?.downloadUrl || null,
        releaseNotes: latestRelease?.releaseNotes || null,
      });
    }

    // Check device limit
    const activeDevices = await db.collection("activations")
      .where("licenseId", "==", license.licenseId)
      .where("status", "==", "active")
      .get();

    if (activeDevices.size >= license.maxDevices) {
      const deviceList = activeDevices.docs.map(d => d.data().deviceName || "Unknown device");
      return res.status(403).json({
        error: `Device limit reached (${license.maxDevices}). Deactivate another device first.`,
        activeDevices: deviceList,
      });
    }

    // Create activation
    await db.collection("activations").add({
      licenseId: license.licenseId,
      licenseKey: cleanKey,
      deviceId,
      deviceName: deviceName || "Unknown Mac",
      activatedAt: now,
      lastSeenAt: now,
      status: "active",
    });

    // Update license activation count
    await licDoc.ref.update({
      activationCount: admin.firestore.FieldValue.increment(1),
      lastActivatedAt: now,
    });

    // Sign token
    const token = signToken(cleanKey, deviceId, license.buyerName, license.maxDevices);

    // Check for updates
    const latestRelease = await getLatestRelease();

    return res.json({
      success: true,
      token,
      licensedTo: license.buyerName,
      maxDevices: license.maxDevices,
      activationCount: license.activationCount + 1,
      latestVersion: latestRelease?.version || null,
      downloadUrl: latestRelease?.downloadUrl || null,
      releaseNotes: latestRelease?.releaseNotes || null,
    });
  } catch (err) {
    console.error("activate error:", err);
    return res.status(500).json({ error: "Server error. Please try again." });
  }
});

// ── POST /api/validate ────────────────────────────────────────────────────────
app.post("/api/validate", async (req, res) => {
  try {
    const { token, deviceId, appVersion } = req.body;

    if (!token || !deviceId) {
      return res.status(400).json({ error: "Token and device ID are required." });
    }

    // Verify the JWT
    let payload;
    try {
      payload = jwt.verify(token, JWT_SECRET);
    } catch (err) {
      return res.status(401).json({ error: "Invalid or expired token.", expired: err.name === "TokenExpiredError" });
    }

    // Find the license
    const licSnap = await db.collection("licenses")
      .where("licenseKey", "==", payload.licenseKey)
      .limit(1)
      .get();

    if (licSnap.empty) {
      return res.status(404).json({ error: "License not found." });
    }

    const license = licSnap.docs[0].data();

    if (license.status !== "active") {
      return res.status(403).json({ error: `License is ${license.status}.`, revoked: true });
    }

    // Update last seen
    const actSnap = await db.collection("activations")
      .where("licenseId", "==", license.licenseId)
      .where("deviceId", "==", deviceId)
      .where("status", "==", "active")
      .limit(1)
      .get();

    const now = new Date().toISOString();
    if (!actSnap.empty) {
      await actSnap.docs[0].ref.update({ lastSeenAt: now, appVersion: appVersion || null });
    }

    // Issue a fresh token
    const newToken = signToken(payload.licenseKey, deviceId, license.buyerName, license.maxDevices);

    // Check for updates
    const latestRelease = await getLatestRelease();

    return res.json({
      valid: true,
      token: newToken,
      licensedTo: license.buyerName,
      maxDevices: license.maxDevices,
      latestVersion: latestRelease?.version || null,
      minRequiredVersion: latestRelease?.minRequiredVersion || null,
      downloadUrl: latestRelease?.downloadUrl || null,
      releaseNotes: latestRelease?.releaseNotes || null,
      mandatory: latestRelease?.mandatory || false,
    });
  } catch (err) {
    console.error("validate error:", err);
    return res.status(500).json({ error: "Server error." });
  }
});

// ── POST /api/deactivate ──────────────────────────────────────────────────────
app.post("/api/deactivate", async (req, res) => {
  try {
    const { licenseKey, deviceId } = req.body;

    if (!licenseKey || !deviceId) {
      return res.status(400).json({ error: "License key and device ID are required." });
    }

    const cleanKey = String(licenseKey).trim().toUpperCase();

    // Find active activation
    const actSnap = await db.collection("activations")
      .where("licenseKey", "==", cleanKey)
      .where("deviceId", "==", deviceId)
      .where("status", "==", "active")
      .limit(1)
      .get();

    if (actSnap.empty) {
      return res.status(404).json({ error: "No active activation found for this device." });
    }

    // Deactivate
    await actSnap.docs[0].ref.update({
      status: "deactivated",
      deactivatedAt: new Date().toISOString(),
    });

    // Decrement activation count on license
    const licSnap = await db.collection("licenses")
      .where("licenseKey", "==", cleanKey)
      .limit(1)
      .get();

    if (!licSnap.empty) {
      await licSnap.docs[0].ref.update({
        activationCount: admin.firestore.FieldValue.increment(-1),
      });
    }

    return res.json({ success: true, message: "Device deactivated successfully." });
  } catch (err) {
    console.error("deactivate error:", err);
    return res.status(500).json({ error: "Server error." });
  }
});

// ── Admin Endpoints ───────────────────────────────────────────────────────────

app.post("/api/admin/licenses", requireAdmin, async (req, res) => {
  try {
    const { search } = req.body;
    let query = db.collection("licenses").orderBy("createdAt", "desc").limit(50);

    const snap = await query.get();
    let licenses = snap.docs.map(d => ({ id: d.id, ...d.data() }));

    // Client-side filter if search term provided
    if (search) {
      const s = search.toLowerCase();
      licenses = licenses.filter(l =>
        l.licenseKey?.toLowerCase().includes(s) ||
        l.buyerName?.toLowerCase().includes(s) ||
        l.buyerEmail?.toLowerCase().includes(s)
      );
    }

    return res.json({ licenses });
  } catch (err) {
    console.error("admin/licenses error:", err);
    return res.status(500).json({ error: "Server error." });
  }
});

app.post("/api/admin/reset", requireAdmin, async (req, res) => {
  try {
    const { licenseId } = req.body;
    if (!licenseId) return res.status(400).json({ error: "licenseId required." });

    // Deactivate all devices for this license
    const actSnap = await db.collection("activations")
      .where("licenseId", "==", licenseId)
      .where("status", "==", "active")
      .get();

    const batch = db.batch();
    actSnap.docs.forEach(doc => {
      batch.update(doc.ref, { status: "deactivated", deactivatedAt: new Date().toISOString() });
    });

    // Reset activation count
    const licRef = db.collection("licenses").doc(licenseId);
    batch.update(licRef, { activationCount: 0 });

    await batch.commit();

    return res.json({ success: true, deactivated: actSnap.size });
  } catch (err) {
    console.error("admin/reset error:", err);
    return res.status(500).json({ error: "Server error." });
  }
});

app.post("/api/admin/revoke", requireAdmin, async (req, res) => {
  try {
    const { licenseId, reason } = req.body;
    if (!licenseId) return res.status(400).json({ error: "licenseId required." });

    await db.collection("licenses").doc(licenseId).update({
      status: "revoked",
      revokedAt: new Date().toISOString(),
      revokeReason: reason || "Revoked by admin",
    });

    // Deactivate all devices
    const actSnap = await db.collection("activations")
      .where("licenseId", "==", licenseId)
      .where("status", "==", "active")
      .get();

    const batch = db.batch();
    actSnap.docs.forEach(doc => {
      batch.update(doc.ref, { status: "revoked", deactivatedAt: new Date().toISOString() });
    });
    await batch.commit();

    return res.json({ success: true, devicesRevoked: actSnap.size });
  } catch (err) {
    console.error("admin/revoke error:", err);
    return res.status(500).json({ error: "Server error." });
  }
});

app.post("/api/admin/release", requireAdmin, async (req, res) => {
  try {
    const { version, downloadUrl, releaseNotes, minRequiredVersion, mandatory } = req.body;
    if (!version || !downloadUrl) {
      return res.status(400).json({ error: "version and downloadUrl are required." });
    }

    await db.collection("app_releases").add({
      version,
      downloadUrl,
      releaseNotes: releaseNotes || "",
      minRequiredVersion: minRequiredVersion || "1.0.0",
      mandatory: mandatory || false,
      releasedAt: new Date().toISOString(),
    });

    return res.json({ success: true, version });
  } catch (err) {
    console.error("admin/release error:", err);
    return res.status(500).json({ error: "Server error." });
  }
});

// ── Export ─────────────────────────────────────────────────────────────────────
exports.api = functions.https.onRequest(app);
