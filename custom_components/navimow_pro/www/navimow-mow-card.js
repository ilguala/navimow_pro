/*
 * Navimow (Private) — "Mow now" card.
 *
 * The card itself is just a "Mow" button; clicking it opens a popup (a modal
 * dialog) where you pick a zone (or all) and choose restart-vs-continue
 * ("riparti da zero"), then confirm with Start. The modal doubles as the
 * confirm step, so a stray tap never sends the robot out.
 *
 * Zero external dependencies (vanilla custom element + a self-built overlay
 * styled with HA CSS variables). Reads the available zones from the schedule
 * sensor's `zones` attribute. Backed by the navimow_pro.mow service.
 */

const STRINGS = {
  en: {
    title: "Mow now",
    button: "Mow",
    allZones: "All zones",
    zone: "Zone",
    reset: "Restart from scratch",
    resetHint: "On: re-mow the whole zone. Off: continue only the uncut area.",
    start: "Start",
    cancel: "Cancel",
    starting: "Starting…",
    started: "Mowing started",
    error: "Start failed",
    noSensor: "Schedule sensor not found.",
    noZones: "No zones known yet.",
  },
  it: {
    title: "Taglia adesso",
    button: "Taglia",
    allZones: "Tutte le zone",
    zone: "Zona",
    reset: "Riparti da zero",
    resetHint: "On: ritaglia tutta la zona. Off: continua solo la parte non tagliata.",
    start: "Avvia",
    cancel: "Annulla",
    starting: "Avvio…",
    started: "Taglio avviato",
    error: "Avvio non riuscito",
    noSensor: "Sensore schedule non trovato.",
    noZones: "Nessuna zona disponibile.",
  },
};

const ALL = "__all__";

class NavimowMowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._zones = [];
    this._sig = null;
    this._sel = ALL;
    this._reset = true;
    this._open = false;
    this._status = null;
    this._rendered = false;
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("navimow-mow-card: `entity` (a *_schedule sensor) is required");
    }
    this._config = { title: null, ...config };
    this._rendered = false;
    this._sig = null;
  }

  static getStubConfig(hass) {
    const match = Object.keys(hass.states || {}).find(
      (e) =>
        e.startsWith("sensor.") &&
        e.endsWith("_schedule") &&
        hass.entities?.[e]?.platform === "navimow_pro"
    );
    return { entity: match || "sensor.navimow_schedule" };
  }

  getCardSize() {
    return 1;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    const st = hass.states[this._config.entity];
    if (!st) {
      this._renderMessage(this._t().noSensor);
      return;
    }
    const zones = Array.isArray(st.attributes.zones) ? st.attributes.zones : [];
    const sig = JSON.stringify(zones);
    if (!this._rendered || sig !== this._sig) {
      this._sig = sig;
      this._zones = zones;
      if (this._sel !== ALL && !zones.some((z) => z.id === this._sel)) this._sel = ALL;
      this._render();
    }
  }
  get hass() {
    return this._hass;
  }

  _lang() {
    const l = (this._hass?.language || "en").toLowerCase();
    return l.startsWith("it") ? "it" : "en";
  }
  _t() {
    return STRINGS[this._lang()];
  }
  _deviceId() {
    return this._hass?.entities?.[this._config.entity]?.device_id || null;
  }
  _cardTitle() {
    return this._config?.title || this._t().title;
  }

  _renderMessage(msg) {
    this.shadowRoot.innerHTML = `<ha-card><div class="pad">${this._esc(msg)}</div></ha-card>${this._style()}`;
    this._rendered = false;
  }

  _render() {
    const t = this._t();
    const s = this._status;
    const statusHtml = s ? `<div class="status ${s.kind}">${this._esc(s.text)}</div>` : "";
    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="cardbody">
          <button class="mowbtn" data-act="open" ${this._zones.length ? "" : "disabled"}>
            <ha-icon icon="mdi:robot-mower"></ha-icon><span>${this._esc(this._cardTitle())}</span>
          </button>
          ${statusHtml}
        </div>
      </ha-card>
      ${this._open ? this._overlay() : ""}
      ${this._style()}
    `;
    this._attach();
    this._rendered = true;
  }

  _overlay() {
    const t = this._t();
    const chips = [
      `<button class="chip ${this._sel === ALL ? "active" : ""}" data-act="zone" data-id="${ALL}">${this._esc(
        t.allZones
      )}</button>`,
    ]
      .concat(
        this._zones.map(
          (z) =>
            `<button class="chip ${this._sel === z.id ? "active" : ""}" data-act="zone" data-id="${z.id}">${this._esc(
              z.name || t.zone + " " + z.id
            )}</button>`
        )
      )
      .join("");
    return `
      <div class="backdrop" data-act="backdrop">
        <div class="dialog" role="dialog" aria-modal="true">
          <div class="dtitle">${this._esc(t.title)}</div>
          <div class="zones">${chips}</div>
          <div class="reset-row">
            <ha-switch data-act="reset" ${this._reset ? "checked" : ""}></ha-switch>
            <div class="reset-txt">
              <div class="reset-name">${this._esc(t.reset)}</div>
              <div class="reset-hint">${this._esc(t.resetHint)}</div>
            </div>
          </div>
          <div class="dactions">
            <button class="cancel" data-act="cancel">${this._esc(t.cancel)}</button>
            <button class="go" data-act="start">${this._esc(t.start)}</button>
          </div>
        </div>
      </div>`;
  }

  _attach() {
    const root = this.shadowRoot;
    const openBtn = root.querySelector("[data-act='open']");
    if (openBtn)
      openBtn.addEventListener("click", () => {
        this._open = true;
        this._status = null;
        this._render();
      });
    const bd = root.querySelector("[data-act='backdrop']");
    if (bd)
      bd.addEventListener("click", (e) => {
        if (e.target === bd) this._close();
      });
    root.querySelectorAll("[data-act='zone']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const id = e.currentTarget.dataset.id;
        this._sel = id === ALL ? ALL : Number(id);
        this._render();
      })
    );
    const sw = root.querySelector("[data-act='reset']");
    if (sw) {
      sw.checked = this._reset;
      sw.addEventListener("change", (e) => {
        this._reset = e.target.checked;
      });
    }
    const cancel = root.querySelector("[data-act='cancel']");
    if (cancel) cancel.addEventListener("click", () => this._close());
    const start = root.querySelector("[data-act='start']");
    if (start) start.addEventListener("click", () => this._mow());
  }

  _close() {
    this._open = false;
    this._render();
  }

  async _mow() {
    const t = this._t();
    this._open = false;
    this._status = { kind: "saving", text: t.starting };
    this._render();
    const data = { reset: this._reset };
    if (this._sel !== ALL) data.zones = [this._sel];
    const dev = this._deviceId();
    if (dev) data.device_id = dev;
    try {
      await this._hass.callService("navimow_pro", "mow", data);
      this._status = { kind: "saved", text: t.started };
    } catch (err) {
      this._status = { kind: "error", text: t.error };
      // eslint-disable-next-line no-console
      console.error("navimow-mow-card: mow failed", err);
    }
    this._render();
  }

  _esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _style() {
    return `<style>
      :host { display: block; }
      .pad { padding: 12px 16px; color: var(--secondary-text-color); }
      .cardbody { padding: 12px; }
      .mowbtn {
        width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px;
        border: none; border-radius: 10px; padding: 12px; cursor: pointer; font-size: 1rem;
        background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff);
      }
      .mowbtn[disabled] { opacity: 0.45; cursor: default; }
      .mowbtn ha-icon { --mdc-icon-size: 22px; }
      .status { margin-top: 10px; font-size: 0.85rem; text-align: center; }
      .status.saving { color: var(--primary-color); }
      .status.saved { color: var(--success-color, #43a047); }
      .status.error { color: var(--error-color, #db4437); }

      /* popup */
      .backdrop {
        position: fixed; inset: 0; z-index: 9999;
        background: rgba(0,0,0,0.45);
        display: flex; align-items: center; justify-content: center; padding: 16px;
      }
      .dialog {
        width: 100%; max-width: 360px; box-sizing: border-box;
        background: var(--card-background-color, var(--ha-card-background, #fff));
        color: var(--primary-text-color, #212121);
        border-radius: 14px; padding: 18px; box-shadow: 0 8px 32px rgba(0,0,0,0.35);
      }
      .dtitle { font-size: 1.15rem; font-weight: 600; margin-bottom: 14px; }
      .zones { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
      .chip {
        border: 1px solid var(--primary-color, #03a9f4);
        background: transparent; color: var(--primary-color, #03a9f4);
        border-radius: 16px; padding: 6px 14px; font-size: 0.9rem; cursor: pointer;
      }
      .chip.active { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
      .reset-row { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
      ha-switch { flex: none; }
      .reset-name { font-weight: 500; }
      .reset-hint { font-size: 0.78rem; color: var(--secondary-text-color); }
      .dactions { display: flex; justify-content: flex-end; gap: 10px; }
      .go {
        border: none; border-radius: 8px; padding: 9px 20px; cursor: pointer; font-size: 0.95rem;
        background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff);
      }
      .cancel {
        border: 1px solid var(--divider-color, #ccc); background: transparent;
        color: var(--secondary-text-color); border-radius: 8px; padding: 9px 16px; cursor: pointer;
      }
    </style>`;
  }
}

if (!customElements.get("navimow-mow-card")) {
  customElements.define("navimow-mow-card", NavimowMowCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "navimow-mow-card",
  name: "Navimow Mow Now",
  description: "Start mowing now (zone + restart/continue) via a popup, for the Navimow (Private) integration.",
  preview: false,
});
