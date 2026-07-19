/*
 * Xiaomi Android Vacuum Map Card
 *
 * A dependency-free Lovelace card for the deterministic Android/Xiaomi vacuum
 * bridge.  It deliberately never starts a job automatically: Preview sends a
 * dry-run request, while Start always requires a second confirmation click.
 */

const CARD_TYPE = "xiaomi-android-vacuum-map";
const SERVICE_DEFAULT = "xiaomi_android_vacuum.start_zone";
const REFRESH_SERVICE_DEFAULT = "xiaomi_android_vacuum.refresh_map";
const NORMALIZED_MAX = 10000;
const MIN_RECTANGLE_AREA = 40000;

const DEFAULT_SAFE_AREA = Object.freeze({
  // The X20+ Android workflow only permits drawing inside this stable part of
  // the 1080 × 2400 Xiaomi Home screenshot. Values are normalized to 0..10000.
  x1: 0,
  y1: 1254,
  x2: 10000,
  y2: 7067,
});

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function isTruthyState(state) {
  return ["on", "true", "yes", "1", "detected"].includes(String(state ?? "").toLowerCase());
}

function friendlyState(value) {
  if (!value || ["unknown", "unavailable", "none"].includes(String(value).toLowerCase())) {
    return "Unknown";
  }
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function normalizedRectangle(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const rectangle = {};
  for (const key of ["x1", "y1", "x2", "y2"]) {
    const number = Number(value[key]);
    if (!Number.isFinite(number)) {
      return null;
    }
    rectangle[key] = Math.round(clamp(number, 0, NORMALIZED_MAX));
  }
  if (rectangle.x1 >= rectangle.x2 || rectangle.y1 >= rectangle.y2) {
    return null;
  }
  return rectangle;
}

function rectangleStyle(rectangle) {
  return [
    `left:${rectangle.x1 / 100}%`,
    `top:${rectangle.y1 / 100}%`,
    `width:${(rectangle.x2 - rectangle.x1) / 100}%`,
    `height:${(rectangle.y2 - rectangle.y1) / 100}%`,
  ].join(";");
}

function splitService(value, fallback) {
  const candidate = String(value || fallback);
  const [domain, service] = candidate.split(".", 2);
  if (!domain || !service) {
    throw new Error(`Service must use domain.service form: ${candidate}`);
  }
  return { domain, service };
}

class XiaomiAndroidVacuumMapCard extends HTMLElement {
  static getStubConfig() {
    return {
      type: `custom:${CARD_TYPE}`,
      entity: "vacuum.xiaomi_robot_vacuum_x20",
      map_image_entity: "image.xiaomi_robot_vacuum_x20_map",
    };
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Card configuration is required");
    }
    if (!config.entity) {
      throw new Error("Set entity to the Xiaomi Android Vacuum vacuum entity");
    }
    this._config = {
      title: "Sui the Hooverbot",
      service: SERVICE_DEFAULT,
      refresh_map_service: REFRESH_SERVICE_DEFAULT,
      max_rectangles: 2,
      safe_area: DEFAULT_SAFE_AREA,
      zones: [],
      ...config,
    };
    this._rectangles = [];
    this._selectedZone = null;
    this._draft = null;
    this._startConfirmationUntil = 0;
    this._message = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  connectedCallback() {
    this._render();
  }

  disconnectedCallback() {
    this._clearConfirmationTimer();
  }

  getCardSize() {
    return 9;
  }

  _state(entityId) {
    return entityId ? this._hass?.states?.[entityId] : undefined;
  }

  _safeArea() {
    const configured = normalizedRectangle(this._config?.safe_area);
    return configured || { ...DEFAULT_SAFE_AREA };
  }

  _zones() {
    const supplied = Array.isArray(this._config?.zones) ? this._config.zones : [];
    const gatewayZones = this._state(this._config?.entity)?.attributes?.known_zones || {};
    const authoritative = Object.entries(gatewayZones)
      .filter(([id, rectangle]) => id && normalizedRectangle(rectangle))
      .map(([id, rectangle]) => ({
        id: String(id),
        label: String(id).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()),
        rectangle,
      }));
    const configured = supplied
      .map((zone) => ({
        id: String(zone?.id ?? zone?.name ?? ""),
        label: String(zone?.label ?? zone?.name ?? zone?.id ?? ""),
        rectangle: normalizedRectangle(zone?.rectangle),
      }))
      .filter((zone) => zone.id && zone.label);
    // The gateway owns which named routines exist. Dashboard config may only
    // improve labels/visible rectangles for those routines.
    const merged = new Map(authoritative.map((zone) => [zone.id, zone]));
    for (const zone of configured) {
      if (merged.has(zone.id)) {
        const previous = merged.get(zone.id);
        merged.set(zone.id, {
          ...previous,
          label: zone.label || previous.label,
          rectangle: zone.rectangle || previous.rectangle,
        });
      }
    }
    return [...merged.values()];
  }

  _mapState() {
    return this._state(this._config?.map_image_entity);
  }

  _mapUrl() {
    const attributes = this._mapState()?.attributes || {};
    const raw = attributes.entity_picture || attributes.image_url || attributes.url || "";
    if (!raw) {
      return "";
    }
    const generation = this._mapGeneration();
    if (!generation || raw.startsWith("data:")) {
      return raw;
    }
    return `${raw}${raw.includes("?") ? "&" : "?"}generation=${encodeURIComponent(generation)}`;
  }

  _mapGeneration() {
    const configured = this._state(this._config?.map_generation_entity)?.state;
    if (configured && !["unknown", "unavailable", "none"].includes(String(configured).toLowerCase())) {
      return String(configured);
    }
    const mapAttributes = this._mapState()?.attributes || {};
    const vacuumAttributes = this._state(this._config?.entity)?.attributes || {};
    return String(
      mapAttributes.map_generation ||
        mapAttributes.generation ||
        vacuumAttributes.map_generation ||
        "",
    );
  }

  _phoneBusy() {
    const configured = this._state(this._config?.phone_busy_entity)?.state;
    if (this._config?.phone_busy_entity) {
      return isTruthyState(configured);
    }
    return Boolean(this._state(this._config?.entity)?.attributes?.phone_busy);
  }

  _needsAttention() {
    const configured = this._state(this._config?.needs_attention_entity)?.state;
    if (this._config?.needs_attention_entity) {
      return isTruthyState(configured);
    }
    const vacuum = this._state(this._config?.entity);
    return Boolean(vacuum?.attributes?.needs_attention) || vacuum?.state === "error";
  }

  _gatewayState() {
    const configured = this._state(this._config?.gateway_state_entity)?.state;
    if (configured && !["unknown", "unavailable", "none"].includes(String(configured).toLowerCase())) {
      return String(configured);
    }
    const attributes = this._state(this._config?.entity)?.attributes || {};
    return String(attributes.workflow_state || attributes.foreground_package || "");
  }

  _isAvailable() {
    const state = this._state(this._config?.entity)?.state;
    return Boolean(state) && !["unavailable", "unknown"].includes(state);
  }

  _zoneReady() {
    return this._state(this._config?.entity)?.attributes?.zone_ready === true;
  }

  _canSubmit() {
    if (this._phoneBusy() || this._needsAttention() || !this._isAvailable()) {
      return false;
    }
    if (!this._zoneReady()) {
      return false;
    }
    if (!this._selectedZone && !this._rectangles.length) {
      return false;
    }
    return Boolean(this._mapGeneration());
  }

  _blockedReason() {
    if (this._phoneBusy()) {
      return "The Android phone is busy, so no action will be sent.";
    }
    if (this._needsAttention()) {
      return "The vacuum needs attention. Resolve it in Xiaomi Home before starting another zone.";
    }
    if (!this._isAvailable()) {
      return "The Home Assistant vacuum entity is unavailable.";
    }
    if (!this._zoneReady() && this._mapGeneration()) {
      return "This map is view-only because its viewport does not match the approved cleaning geometry. Drawing is safe, but Preview and Start are disabled.";
    }
    if ((this._selectedZone || this._rectangles.length) && !this._mapGeneration()) {
      return "Refresh the map before validating or starting any zone.";
    }
    if (!this._selectedZone && !this._rectangles.length) {
      return "Choose a saved zone or draw a rectangle on the map.";
    }
    return "";
  }

  _render() {
    if (!this._config) {
      return;
    }
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
    const vacuum = this._state(this._config.entity);
    const mapUrl = this._mapUrl();
    const safeArea = this._safeArea();
    const zones = this._zones();
    const mapGeneration = this._mapGeneration();
    const blockedReason = this._blockedReason();
    const submitAllowed = this._canSubmit();
    const confirmationActive = this._startConfirmationUntil > Date.now();
    const gatewayState = this._gatewayState();
    const selectedLabel = this._selectedZone
      ? zones.find((zone) => zone.id === this._selectedZone)?.label || this._selectedZone
      : "";
    const status = friendlyState(vacuum?.state);
    const stateClass = String(vacuum?.state || "unknown").replace(/[^a-z0-9_-]/gi, "");

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { overflow: hidden; }
        .header { display: flex; align-items: flex-start; gap: 12px; padding: 16px 16px 10px; }
        .header-main { min-width: 0; flex: 1; }
        .title { color: var(--primary-text-color); font-size: 1.15em; font-weight: 500; }
        .subtitle { color: var(--secondary-text-color); font-size: .9em; margin-top: 3px; }
        .state { border-radius: 999px; background: var(--secondary-background-color); color: var(--primary-text-color); font-size: .8em; font-weight: 600; padding: 5px 9px; text-transform: capitalize; white-space: nowrap; }
        .state.cleaning { background: var(--success-color, #2e7d32); color: white; }
        .state.error { background: var(--error-color, #db4437); color: white; }
        .state.paused, .state.returning { background: var(--warning-color, #ff9800); color: #1d1d1d; }
        .notice { margin: 0 16px 12px; padding: 9px 11px; border-radius: 8px; background: var(--warning-color, #ff9800); color: #1d1d1d; font-size: .9em; }
        .notice.error { background: var(--error-color, #db4437); color: white; }
        .notice.info { background: var(--secondary-background-color); color: var(--primary-text-color); }
        .map-wrap { background: #111; border-top: 1px solid var(--divider-color); border-bottom: 1px solid var(--divider-color); }
        .map-stage { position: relative; width: 100%; min-height: 260px; overflow: hidden; touch-action: none; user-select: none; }
        .map-stage.draw-enabled { cursor: crosshair; }
        /* Keep the screenshot's aspect ratio: the drawing overlay is mapped
           directly to this full image, rather than to a cropped viewport. */
        .map-image { display: block; width: 100%; height: auto; background: #111; }
        .placeholder { align-items: center; box-sizing: border-box; color: #e0e0e0; display: flex; flex-direction: column; gap: 8px; justify-content: center; min-height: 260px; padding: 28px; text-align: center; }
        .placeholder small { color: #aaa; max-width: 34ch; }
        .drawing-layer { inset: 0; position: absolute; }
        .safe-area { border: 2px dashed rgba(255, 222, 77, .86); box-sizing: border-box; pointer-events: none; position: absolute; }
        .safe-label { background: rgba(21, 21, 21, .78); color: #ffe36a; font-size: 11px; left: 0; padding: 3px 5px; position: absolute; top: 0; }
        .rectangle { background: rgba(52, 152, 219, .24); border: 2px solid #2f9de0; box-sizing: border-box; pointer-events: none; position: absolute; }
        .rectangle .number { background: #2f9de0; border-radius: 50%; color: white; font-size: 11px; font-weight: 600; height: 20px; left: -1px; line-height: 20px; position: absolute; text-align: center; top: -1px; width: 20px; }
        .rectangle.draft { background: rgba(255, 193, 7, .22); border-color: #ffca28; }
        .named-zone { background: rgba(156, 39, 176, .12); border: 2px dashed rgba(218, 137, 236, .95); box-sizing: border-box; pointer-events: none; position: absolute; }
        .named-zone.selected { background: rgba(218, 137, 236, .26); border-style: solid; }
        .controls { padding: 14px 16px 16px; }
        .section-label { color: var(--secondary-text-color); display: block; font-size: .83em; font-weight: 600; letter-spacing: .04em; margin-bottom: 8px; text-transform: uppercase; }
        .zone-list, .actions, .draw-actions { display: flex; flex-wrap: wrap; gap: 8px; }
        button { align-items: center; background: var(--secondary-background-color); border: 0; border-radius: 8px; color: var(--primary-text-color); cursor: pointer; display: inline-flex; font: inherit; gap: 6px; min-height: 36px; padding: 7px 11px; }
        button:hover:not(:disabled) { background: var(--divider-color); }
        button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        button:disabled { cursor: not-allowed; opacity: .5; }
        button.selected { background: var(--primary-color); color: var(--text-primary-color, white); }
        .draw-actions { align-items: center; margin: 12px 0; }
        .draw-summary { color: var(--secondary-text-color); font-size: .9em; flex: 1 1 160px; }
        .actions { border-top: 1px solid var(--divider-color); margin-top: 14px; padding-top: 14px; }
        .primary { background: var(--primary-color); color: var(--text-primary-color, white); }
        .primary.confirm { background: var(--error-color, #db4437); }
        .secondary { margin-left: auto; }
        .generation { color: var(--secondary-text-color); font-family: var(--code-font-family, monospace); font-size: .75em; margin-top: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .hint { color: var(--secondary-text-color); font-size: .88em; line-height: 1.4; margin: 10px 0 0; }
        .hidden { display: none; }
      </style>
      <ha-card>
        <div class="header">
          <div class="header-main">
            <div class="title">${escapeHtml(this._config.title)}</div>
            <div class="subtitle">${gatewayState ? `Bridge: ${escapeHtml(friendlyState(gatewayState))} · ` : ""}Draw a zone only inside the yellow dashed area.</div>
          </div>
          <div class="state ${escapeHtml(stateClass)}">${escapeHtml(status)}</div>
        </div>
        ${this._noticeHtml(blockedReason)}
        ${this._messageHtml()}
        <div class="map-wrap">
          <div class="map-stage ${mapUrl && !this._phoneBusy() && !this._needsAttention() ? "draw-enabled" : ""}" id="map-stage" aria-label="Vacuum map: drag to draw a cleaning rectangle">
            ${
              mapUrl
                ? `<img class="map-image" draggable="false" src="${escapeHtml(mapUrl)}" alt="Latest Xiaomi Home vacuum map" />`
                : `<div class="placeholder"><strong>No map preview yet</strong><small>Use Refresh map while Xiaomi Home is visible and the Android phone is not busy.</small></div>`
            }
            <div class="drawing-layer" aria-hidden="true">
              <div class="safe-area" style="${rectangleStyle(safeArea)}"><span class="safe-label">safe drawing area</span></div>
              ${zones
                .filter((zone) => zone.rectangle)
                .map(
                  (zone) =>
                    `<div class="named-zone ${zone.id === this._selectedZone ? "selected" : ""}" style="${rectangleStyle(zone.rectangle)}"></div>`,
                )
                .join("")}
              ${this._rectangles
                .map(
                  (rectangle, index) =>
                    `<div class="rectangle" style="${rectangleStyle(rectangle)}"><span class="number">${index + 1}</span></div>`,
                )
                .join("")}
              <div class="rectangle draft hidden"></div>
            </div>
          </div>
        </div>
        <div class="controls">
          ${
            zones.length
              ? `<span class="section-label">Saved zones</span>
                 <div class="zone-list">
                   ${zones
                     .map(
                       (zone, index) =>
                         `<button type="button" class="zone-button ${zone.id === this._selectedZone ? "selected" : ""}" data-zone-index="${index}" aria-pressed="${zone.id === this._selectedZone}">${escapeHtml(zone.label)}</button>`,
                     )
                     .join("")}
                 </div>`
              : ""
          }
          <div class="draw-actions">
            <span class="draw-summary">${escapeHtml(this._selectionSummary(selectedLabel))}</span>
            <button type="button" id="clear-drawn" ${this._rectangles.length ? "" : "disabled"}>Clear drawn</button>
          </div>
          <div class="actions">
            <button type="button" id="refresh-map">Refresh map</button>
            <button type="button" id="preview-zone" ${submitAllowed ? "" : "disabled"}>Preview</button>
            <button type="button" id="start-zone" class="primary ${confirmationActive ? "confirm" : ""}" ${submitAllowed ? "" : "disabled"}>${confirmationActive ? "Confirm start" : "Start"}</button>
          </div>
          <div class="generation">Map generation: ${mapGeneration ? escapeHtml(mapGeneration) : "not available"}</div>
          <p class="hint">Preview validates the zone without starting the vacuum. Start never runs automatically and requires confirmation.</p>
        </div>
      </ha-card>
    `;
    this._bindEvents();
  }

  _noticeHtml(reason) {
    if (!reason) {
      return "";
    }
    const attention = this._needsAttention();
    return `<div class="notice ${attention ? "error" : ""}">${escapeHtml(reason)}</div>`;
  }

  _messageHtml() {
    if (!this._message) {
      return "";
    }
    return `<div class="notice ${escapeHtml(this._message.kind || "info")}">${escapeHtml(this._message.text)}</div>`;
  }

  _selectionSummary(selectedLabel) {
    if (selectedLabel) {
      return `Saved zone selected: ${selectedLabel}.`;
    }
    if (this._rectangles.length) {
      return `${this._rectangles.length} drawn zone${this._rectangles.length === 1 ? "" : "s"} selected.`;
    }
    return "Drag on the map to draw up to two zones.";
  }

  _bindEvents() {
    const root = this.shadowRoot;
    root.querySelector("#refresh-map")?.addEventListener("click", () => this._refreshMap());
    root.querySelector("#preview-zone")?.addEventListener("click", () => this._submitZone(true));
    root.querySelector("#start-zone")?.addEventListener("click", () => this._confirmOrStart());
    root.querySelector("#clear-drawn")?.addEventListener("click", () => {
      this._rectangles = [];
      this._selectedZone = null;
      this._message = null;
      this._render();
    });
    root.querySelectorAll(".zone-button").forEach((button) => {
      button.addEventListener("click", () => {
        const zone = this._zones()[Number(button.dataset.zoneIndex)];
        if (!zone) {
          return;
        }
        this._selectedZone = this._selectedZone === zone.id ? null : zone.id;
        this._rectangles = [];
        this._message = null;
        this._clearConfirmationTimer();
        this._render();
      });
    });
    const stage = root.querySelector("#map-stage");
    if (stage) {
      stage.addEventListener("pointerdown", (event) => this._beginDrag(event, stage));
      stage.addEventListener("pointermove", (event) => this._moveDrag(event, stage));
      stage.addEventListener("pointerup", (event) => this._finishDrag(event, stage));
      stage.addEventListener("pointercancel", () => this._cancelDrag());
    }
  }

  _pointFromEvent(event, stage) {
    const bounds = stage.getBoundingClientRect();
    if (!bounds.width || !bounds.height) {
      return null;
    }
    const safe = this._safeArea();
    return {
      x: Math.round(clamp(((event.clientX - bounds.left) / bounds.width) * NORMALIZED_MAX, safe.x1, safe.x2)),
      y: Math.round(clamp(((event.clientY - bounds.top) / bounds.height) * NORMALIZED_MAX, safe.y1, safe.y2)),
    };
  }

  _beginDrag(event, stage) {
    if (event.button !== 0 || !this._mapUrl() || this._phoneBusy() || this._needsAttention()) {
      return;
    }
    if (this._rectangles.length >= Number(this._config.max_rectangles || 2)) {
      this._setMessage(`Only ${this._config.max_rectangles || 2} rectangles can be sent in one request. Clear one first.`, "info");
      return;
    }
    const point = this._pointFromEvent(event, stage);
    if (!point) {
      return;
    }
    event.preventDefault();
    stage.setPointerCapture?.(event.pointerId);
    this._selectedZone = null;
    this._draft = { start: point, end: point, pointerId: event.pointerId };
    this._message = null;
    this._updateDraftOverlay();
  }

  _moveDrag(event, stage) {
    if (!this._draft || event.pointerId !== this._draft.pointerId) {
      return;
    }
    const point = this._pointFromEvent(event, stage);
    if (!point) {
      return;
    }
    event.preventDefault();
    this._draft.end = point;
    this._updateDraftOverlay();
  }

  _finishDrag(event, stage) {
    if (!this._draft || event.pointerId !== this._draft.pointerId) {
      return;
    }
    const point = this._pointFromEvent(event, stage);
    if (point) {
      this._draft.end = point;
    }
    const rectangle = this._draftRectangle();
    this._draft = null;
    stage.releasePointerCapture?.(event.pointerId);
    if (!rectangle || (rectangle.x2 - rectangle.x1) * (rectangle.y2 - rectangle.y1) < MIN_RECTANGLE_AREA) {
      this._setMessage("That rectangle is too small. Draw a larger zone inside the dashed area.", "info");
      return;
    }
    this._rectangles = [...this._rectangles, rectangle];
    this._clearConfirmationTimer();
    this._render();
  }

  _cancelDrag() {
    if (!this._draft) {
      return;
    }
    this._draft = null;
    this._updateDraftOverlay();
  }

  _draftRectangle() {
    if (!this._draft) {
      return null;
    }
    return normalizedRectangle({
      x1: Math.min(this._draft.start.x, this._draft.end.x),
      y1: Math.min(this._draft.start.y, this._draft.end.y),
      x2: Math.max(this._draft.start.x, this._draft.end.x),
      y2: Math.max(this._draft.start.y, this._draft.end.y),
    });
  }

  _updateDraftOverlay() {
    const element = this.shadowRoot?.querySelector(".rectangle.draft");
    const rectangle = this._draftRectangle();
    if (!element) {
      return;
    }
    if (!rectangle) {
      element.classList.add("hidden");
      return;
    }
    element.classList.remove("hidden");
    element.style.cssText = rectangleStyle(rectangle);
  }

  _setMessage(text, kind = "info") {
    this._message = { text, kind };
    this._render();
  }

  _clearConfirmationTimer() {
    if (this._confirmationTimer) {
      window.clearTimeout(this._confirmationTimer);
      this._confirmationTimer = null;
    }
    this._startConfirmationUntil = 0;
  }

  _confirmOrStart() {
    if (!this._canSubmit()) {
      return;
    }
    if (this._startConfirmationUntil > Date.now()) {
      this._clearConfirmationTimer();
      this._submitZone(false);
      return;
    }
    this._startConfirmationUntil = Date.now() + 8000;
    this._message = { text: "Review the highlighted zone, then click Confirm start within 8 seconds.", kind: "info" };
    this._confirmationTimer = window.setTimeout(() => {
      this._startConfirmationUntil = 0;
      this._confirmationTimer = null;
      this._render();
    }, 8000);
    this._render();
  }

  _serviceData(dryRun) {
    const data = {
      entity_id: this._config.entity,
      dry_run: dryRun,
      map_generation: this._mapGeneration(),
    };
    if (this._selectedZone) {
      data.zone_name = this._selectedZone;
    } else {
      data.rectangles = this._rectangles;
    }
    return data;
  }

  async _submitZone(dryRun) {
    if (!this._canSubmit() || !this._hass?.callService) {
      return;
    }
    try {
      const { domain, service } = splitService(this._config.service, SERVICE_DEFAULT);
      this._setMessage(dryRun ? "Requesting a safe preview…" : "Sending the confirmed cleanup request…", "info");
      await this._hass.callService(domain, service, this._serviceData(dryRun));
      this._setMessage(
        dryRun
          ? "Preview accepted. It did not start the vacuum."
          : "Cleanup request accepted. Watch the vacuum state for confirmation.",
        "info",
      );
    } catch (error) {
      this._setMessage(`The bridge rejected the request: ${error?.message || error}`, "error");
    }
  }

  async _refreshMap() {
    if (!this._hass?.callService) {
      return;
    }
    if (this._phoneBusy()) {
      this._setMessage("The Android phone is busy; map refresh was not requested.", "info");
      return;
    }
    try {
      const { domain, service } = splitService(this._config.refresh_map_service, REFRESH_SERVICE_DEFAULT);
      this._setMessage("Requesting a fresh map preview…", "info");
      await this._hass.callService(domain, service, { entity_id: this._config.entity });
      this._setMessage("Map refresh requested. The image updates once the Android bridge has captured it.", "info");
    } catch (error) {
      this._setMessage(`Could not refresh the map: ${error?.message || error}`, "error");
    }
  }
}

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, XiaomiAndroidVacuumMapCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: `custom:${CARD_TYPE}`,
  name: "Xiaomi Android Vacuum Map",
  description: "Draw and safely start deterministic Xiaomi Home zone-cleaning workflows.",
  preview: false,
});
