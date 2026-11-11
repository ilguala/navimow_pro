/*
 * Navimow (Private) — Map card.
 *
 * Shows the integration's map camera and adds zoom, pan and rotation on top of
 * it, all in the browser: the image itself is unchanged, a CSS transform moves,
 * scales and turns it. Wheel or the +/- buttons zoom (toward the pointer), drag
 * or one finger pans, two fingers pinch and twist, a button turns 90°, and a
 * button recentres. The view is per browser and not sent anywhere; the picture
 * keeps refreshing underneath.
 *
 * The map is laid out large and shrunk to fit, so zooming in stays crisp well
 * past its on-screen size instead of magnifying a small bitmap. The legend and
 * the status line are drawn as fixed overlays from the camera's attributes, so
 * they stay put while the map moves (the camera leaves them off the picture
 * when its "clean map" option is on).
 *
 * Zero external dependencies (vanilla custom element, HA CSS variables). With
 * no `entity` it picks the integration's map camera itself.
 */

const STRINGS = {
  en: { title: "Map", noCam: "No Navimow map camera found.", zin: "Zoom in",
        zout: "Zoom out", rot: "Rotate", reset: "Reset view" },
  it: { title: "Mappa", noCam: "Nessuna camera mappa Navimow trovata.", zin: "Ingrandisci",
        zout: "Riduci", rot: "Ruota", reset: "Ripristina vista" },
  de: { title: "Karte", noCam: "Keine Navimow-Kartenkamera gefunden.", zin: "Vergrößern",
        zout: "Verkleinern", rot: "Drehen", reset: "Ansicht zurücksetzen" },
  fr: { title: "Carte", noCam: "Aucune caméra de carte Navimow trouvée.", zin: "Zoomer",
        zout: "Dézoomer", rot: "Pivoter", reset: "Réinitialiser la vue" },
};

const MIN_ZOOM = 1;
const MAX_ZOOM = 8;
const NATURAL = 1400; // the map is rendered this big, then fit to the card
const REFRESH_MS = 5000;

class NavimowMapCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._zoom = 1;
    this._tx = 0;
    this._ty = 0;
    this._rot = 0;
    this._built = false;
    this._tick = 0;
    this._entity = null;
    this._pointers = new Map();
  }

  setConfig(config) {
    // `default_rotation` is the orientation the map opens at and returns to
    // (degrees clockwise). 0 is north up, as the mower's frame and the app have
    // it. The ⤢ button and the pinch/Ctrl-drag turn freely from there.
    this._config = { title: null, entity: null, default_rotation: 0, ...config };
    this._rot = Number(this._config.default_rotation) || 0;
  }

  static getStubConfig(hass) {
    return { entity: NavimowMapCard._autoCam(hass) || "" };
  }

  static _autoCam(hass) {
    return (
      Object.keys(hass.states).find(
        (e) => e.startsWith("camera.") && /navimow.*mapp|mapp.*navimow/i.test(e)
      ) || null
    );
  }

  getCardSize() {
    return 6;
  }

  _t() {
    const lang = (this._hass?.language || "en").slice(0, 2);
    return STRINGS[lang] || STRINGS.en;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    this._entity = this._config.entity || NavimowMapCard._autoCam(hass);
    if (!this._built) this._build();
    this._refresh();
  }

  _build() {
    const t = this._t();
    this.shadowRoot.innerHTML = `
      <ha-card header="${this._config.title ?? t.title}">
        <div class="stage">
          <img class="map" alt="map" draggable="false"/>
          <div class="legend"></div>
          <div class="status"></div>
          <div class="empty">${t.noCam}</div>
          <div class="ctrls">
            <button class="btn zin" title="${t.zin}">+</button>
            <button class="btn zout" title="${t.zout}">−</button>
            <button class="btn rot" title="${t.rot}">↻</button>
            <button class="btn rst" title="${t.reset}">⤢</button>
          </div>
        </div>
      </ha-card>
      ${this._style()}`;
    this._stage = this.shadowRoot.querySelector(".stage");
    this._img = this.shadowRoot.querySelector(".map");
    this._legend = this.shadowRoot.querySelector(".legend");
    this._statusEl = this.shadowRoot.querySelector(".status");
    this._empty = this.shadowRoot.querySelector(".empty");
    this.shadowRoot.querySelector(".zin").onclick = () => this._zoomBy(1.4, null);
    this.shadowRoot.querySelector(".zout").onclick = () => this._zoomBy(1 / 1.4, null);
    this.shadowRoot.querySelector(".rot").onclick = () => { this._rot = (this._rot + 90) % 360; this._apply(); };
    this.shadowRoot.querySelector(".rst").onclick = () => this._resetView();

    this._stage.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      this._zoomBy(ev.deltaY < 0 ? 1.15 : 1 / 1.15, ev);
    }, { passive: false });

    this._stage.addEventListener("pointerdown", (ev) => {
      if (ev.target.classList.contains("btn")) return;
      this._stage.setPointerCapture(ev.pointerId);
      this._pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (this._pointers.size === 1 && (ev.ctrlKey || ev.metaKey)) {
        // Hold Ctrl (or Cmd) and drag to turn the map by any angle.
        this._rotdrag = { ang: this._centreAngle(ev.clientX, ev.clientY), rot: this._rot };
        this._drag = null;
      } else if (this._pointers.size === 1) {
        this._drag = { x: ev.clientX, y: ev.clientY, tx: this._tx, ty: this._ty };
      } else if (this._pointers.size === 2) {
        this._drag = null;
        this._pinch = this._pinchState();
      }
    });
    this._stage.addEventListener("pointermove", (ev) => {
      if (!this._pointers.has(ev.pointerId)) return;
      this._pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (this._pinch && this._pointers.size >= 2) {
        const now = this._pinchState();
        let da = now.ang - this._pinch.ang;
        if (da > 180) da -= 360; else if (da < -180) da += 360;
        this._rot += da;
        this._zoomBy(now.dist / this._pinch.dist, { clientX: now.cx, clientY: now.cy });
        this._pinch = now;
      } else if (this._rotdrag) {
        this._rot = this._rotdrag.rot + (this._centreAngle(ev.clientX, ev.clientY) - this._rotdrag.ang);
        this._apply();
      } else if (this._drag) {
        this._tx = this._drag.tx + (ev.clientX - this._drag.x);
        this._ty = this._drag.ty + (ev.clientY - this._drag.y);
        this._apply();
      }
    });
    const up = (ev) => {
      this._pointers.delete(ev.pointerId);
      if (this._pointers.size < 2) this._pinch = null;
      if (this._pointers.size === 0) { this._drag = null; this._rotdrag = null; }
    };
    this._stage.addEventListener("pointerup", up);
    this._stage.addEventListener("pointercancel", up);
    this._built = true;
  }

  _fit() {
    const r = this._stage.getBoundingClientRect();
    const s = Math.min(r.width, r.height) / NATURAL;
    return s > 0 ? s : 1;
  }

  _centreAngle(cx, cy) {
    const r = this._stage.getBoundingClientRect();
    return (Math.atan2(cy - (r.top + r.height / 2), cx - (r.left + r.width / 2)) * 180) / Math.PI;
  }

  _pinchState() {
    const pts = [...this._pointers.values()];
    const dx = pts[0].x - pts[1].x;
    const dy = pts[0].y - pts[1].y;
    return {
      dist: Math.hypot(dx, dy) || 1,
      ang: (Math.atan2(dy, dx) * 180) / Math.PI,
      cx: (pts[0].x + pts[1].x) / 2,
      cy: (pts[0].y + pts[1].y) / 2,
    };
  }

  _zoomBy(factor, ev) {
    const prev = this._zoom;
    const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev * factor));
    if (next === prev) return;
    const rect = this._stage.getBoundingClientRect();
    const ox = ev ? ev.clientX - rect.left - rect.width / 2 : 0;
    const oy = ev ? ev.clientY - rect.top - rect.height / 2 : 0;
    const k = next / prev;
    this._tx = ox - (ox - this._tx) * k;
    this._ty = oy - (oy - this._ty) * k;
    this._zoom = next;
    this._clamp();
    this._apply();
  }

  _resetView() {
    this._zoom = 1;
    this._tx = 0;
    this._ty = 0;
    this._rot = Number(this._config?.default_rotation) || 0;
    this._apply();
  }

  _clamp() {
    const r = this._stage.getBoundingClientRect();
    const mx = (r.width * (this._zoom - 1)) / 2;
    const my = (r.height * (this._zoom - 1)) / 2;
    this._tx = Math.max(-mx, Math.min(mx, this._tx));
    this._ty = Math.max(-my, Math.min(my, this._ty));
  }

  _apply() {
    if (!this._img) return;
    const s = this._fit() * this._zoom;
    this._img.style.transform =
      `translate(${this._tx.toFixed(1)}px, ${this._ty.toFixed(1)}px) ` +
      `scale(${s.toFixed(4)}) rotate(${this._rot.toFixed(1)}deg)`;
  }

  _refresh() {
    const st = this._entity ? this._hass.states[this._entity] : null;
    const pic = st?.attributes?.entity_picture;
    if (!pic) {
      this._empty.style.display = "grid";
      this._img.style.display = "none";
      this._legend.innerHTML = "";
      this._statusEl.textContent = "";
      return;
    }
    this._empty.style.display = "none";
    this._img.style.display = "block";
    const now = Date.now();
    if (now - this._tick >= REFRESH_MS || this._img.dataset.base !== pic) {
      this._tick = now;
      this._img.dataset.base = pic;
      this._img.onload = () => this._apply();
      this._img.src = pic + (pic.includes("?") ? "&" : "?") + "_n=" + now;
    }
    this._overlays(st.attributes);
  }

  _overlays(attrs) {
    const legend = Array.isArray(attrs?.legend) ? attrs.legend : null;
    if (legend) {
      this._legend.innerHTML = legend
        .map(
          (r) =>
            `<div class="lrow"><span class="sw" style="background:${String(r.color || "#888")
              .replace(/[^#\w(),.% ]/g, "")}"></span>${this._esc(r.label)}</div>`
        )
        .join("");
      this._legend.style.display = legend.length ? "block" : "none";
    } else {
      this._legend.style.display = "none";
    }
    const status = attrs?.status;
    this._statusEl.textContent = status ? String(status) : "";
    this._statusEl.style.display = status ? "block" : "none";
  }

  _esc(s) {
    return String(s == null ? "" : s).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
  }

  _style() {
    return `<style>
      .stage {
        position: relative; width: 100%; aspect-ratio: 1 / 1; overflow: hidden;
        touch-action: none; cursor: grab; background: var(--card-background-color, #fff);
        border-radius: 0 0 var(--ha-card-border-radius, 12px) var(--ha-card-border-radius, 12px);
      }
      .stage:active { cursor: grabbing; }
      .map {
        position: absolute; left: 50%; top: 50%; width: ${NATURAL}px; height: ${NATURAL}px;
        margin-left: ${-NATURAL / 2}px; margin-top: ${-NATURAL / 2}px;
        transform-origin: center center; user-select: none; will-change: transform;
      }
      .empty {
        display: none; position: absolute; inset: 0; place-content: center; text-align: center;
        color: var(--secondary-text-color); padding: 24px;
      }
      .legend {
        position: absolute; left: 8px; top: 8px; pointer-events: none;
        background: var(--card-background-color, #fff); opacity: 0.92;
        border: 1px solid var(--divider-color, #0002); border-radius: 8px;
        padding: 6px 8px; font-size: 0.78rem; color: var(--primary-text-color);
        box-shadow: 0 1px 3px #0002;
      }
      .lrow { display: flex; align-items: center; gap: 6px; line-height: 1.5; }
      .sw { width: 11px; height: 11px; border-radius: 2px; border: 1px solid #fff; box-shadow: 0 0 0 1px #0002; }
      .status {
        position: absolute; left: 8px; bottom: 8px; pointer-events: none;
        background: var(--card-background-color, #fff); opacity: 0.92;
        border: 1px solid var(--divider-color, #0002); border-radius: 8px;
        padding: 4px 10px; font-size: 0.82rem; font-weight: 600; color: var(--primary-text-color);
        box-shadow: 0 1px 3px #0002;
      }
      .ctrls {
        position: absolute; right: 10px; bottom: 10px; display: flex; flex-direction: column; gap: 6px;
      }
      .btn {
        width: 34px; height: 34px; border-radius: 8px; cursor: pointer; font-size: 1.1rem; line-height: 1;
        border: 1px solid var(--divider-color, #0002);
        background: var(--card-background-color, #fff); color: var(--primary-text-color);
        box-shadow: 0 1px 3px #0003;
      }
      .btn:hover { background: var(--secondary-background-color, #eee); }
    </style>`;
  }
}

if (!customElements.get("navimow-map-card")) {
  customElements.define("navimow-map-card", NavimowMapCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "navimow-map-card",
  name: "Navimow Map",
  description: "The Navimow (Private) map with zoom, pan and rotation.",
  preview: false,
});
