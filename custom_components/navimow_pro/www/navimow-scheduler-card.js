/*
 * Navimow (Private) — graphical mowing-schedule card.
 *
 * A weekly, app-like editor for the Segway Navimow mowing plan:
 *   - one section per weekday, with an on/off toggle;
 *   - one or more time periods per day (multiple mowing sessions);
 *   - per-period zone selection (no zones selected = whole map / all zones);
 *   - a per-day "Save" that writes only that day via navimow_pro.set_schedule,
 *     and a per-day "Discard" that reverts unsaved edits for that day.
 *
 * Zero external dependencies (vanilla custom element) so it is robust across
 * Home Assistant frontend versions. It reads everything from ONE entity, the
 * schedule sensor (attributes: `days` = parsed plan, `zones` = available zones),
 * and writes with the integration's set_schedule service.
 *
 * The card is auto-registered by the integration (add_extra_js_url), so no
 * manual Lovelace resource step is normally needed.
 *
 * End-of-day convention: the mower's last slot (96) is 24:00. It round-trips
 * from the backend as "00:00"; this card treats an END time of "00:00" as
 * end-of-day (1440 min), and the set_schedule service applies the same rule, so
 * a "mow until midnight" window stays editable and savable.
 */

const STRINGS = {
  en: {
    title: "Mowing schedule",
    add: "Add period",
    save: "Save",
    discard: "Discard",
    saved: "Saved",
    saving: "Saving...",
    error: "Save failed",
    allZones: "All zones",
    off: "Off",
    remove: "Remove period",
    noSensor: "Schedule sensor not found.",
    invalid: "End must be after start.",
    incomplete: "Fill in both times.",
    slot: "period",
    slots: "periods",
    dash: "&#8594;",
  },
  it: {
    title: "Piano di taglio",
    add: "Aggiungi fascia",
    save: "Salva",
    discard: "Annulla",
    saved: "Salvato",
    saving: "Salvataggio...",
    error: "Salvataggio non riuscito",
    allZones: "Tutte le zone",
    off: "Off",
    remove: "Rimuovi fascia",
    noSensor: "Sensore schedule non trovato.",
    invalid: "La fine deve essere dopo l'inizio.",
    incomplete: "Compila entrambi gli orari.",
    slot: "fascia",
    slots: "fasce",
    dash: "&#8594;",
  },
};

// Display order Monday-first; `num` is the Navimow weekday number (1=Sun..7=Sat),
// `key` is the weekday name the set_schedule service expects.
const DAYS = [
  { num: 2, key: "monday" },
  { num: 3, key: "tuesday" },
  { num: 4, key: "wednesday" },
  { num: 5, key: "thursday" },
  { num: 6, key: "friday" },
  { num: 7, key: "saturday" },
  { num: 1, key: "sunday" },
];

const DAY_LABELS = {
  en: {
    monday: "Monday", tuesday: "Tuesday", wednesday: "Wednesday",
    thursday: "Thursday", friday: "Friday", saturday: "Saturday", sunday: "Sunday",
  },
  it: {
    monday: "Luned&igrave;", tuesday: "Marted&igrave;", wednesday: "Mercoled&igrave;",
    thursday: "Gioved&igrave;", friday: "Venerd&igrave;", saturday: "Sabato", sunday: "Domenica",
  },
};

class NavimowSchedulerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._draft = null;       // [{num,key,enabled,periods:[{start,end,zones:[id]}],_dirty,_saving}]
    this._serverDays = [];    // last-seen `days` attribute (for per-day discard/merge)
    this._zones = [];         // [{id,name}]
    this._sig = null;         // last-seen schedule signature (to detect real changes)
    this._status = {};        // dayKey -> {kind:'saving'|'saved'|'error', text}
    this._clearTimers = {};   // dayKey -> timeout id (auto-clear a 'saved' badge)
    this._rendered = false;
  }

  // ---- Lovelace config -----------------------------------------------------
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("navimow-scheduler-card: `entity` (a *_schedule sensor) is required");
    }
    this._config = { title: null, ...config };
    this._rendered = false;
    this._sig = null;
    this._draft = null;
    this._status = {};
  }

  static getStubConfig(hass) {
    const match = Object.keys(hass.states || {}).find(
      (e) =>
        e.startsWith("sensor.") &&
        e.endsWith("_schedule") &&
        (hass.entities?.[e]?.platform === "navimow_pro")
    );
    return { entity: match || "sensor.navimow_schedule" };
  }

  getCardSize() {
    return 3 + DAYS.length;
  }

  // ---- hass updates --------------------------------------------------------
  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;

    const st = hass.states[this._config.entity];
    if (!st) {
      this._renderMessage(this._t().noSensor);
      return;
    }

    const days = Array.isArray(st.attributes.days) ? st.attributes.days : [];
    const zones = Array.isArray(st.attributes.zones) ? st.attributes.zones : [];
    const sig = JSON.stringify([days, zones]);

    if (!this._rendered) {
      this._sig = sig;
      this._serverDays = days;
      this._zones = zones;
      this._draft = this._buildDraft(days);
      this._render();
      return;
    }

    if (sig === this._sig) return; // nothing relevant changed on the server

    // A genuine server-side plan change arrived. Never rebuild while the user
    // is focused inside the card — that would steal focus or discard a
    // half-entered value. Leave _sig unchanged so a later (unfocused) poll
    // still applies the change.
    if (this.shadowRoot.activeElement) return;

    this._sig = sig;
    this._serverDays = days;
    this._zones = zones;
    // Merge: refresh days with NO unsaved edits from the server; keep days that
    // the user is still editing. So one dirty day no longer freezes the rest.
    // Carry the accordion expand state so a poll never collapses an open day.
    this._draft = this._draft.map((d, i) =>
      d._dirty || d._saving ? d : { ...this._buildDayDraft(days, i), _expanded: d._expanded }
    );
    this._render();
  }

  get hass() {
    return this._hass;
  }

  // ---- helpers -------------------------------------------------------------
  _lang() {
    const l = (this._hass?.language || "en").toLowerCase();
    return l.startsWith("it") ? "it" : "en";
  }
  _t() {
    return STRINGS[this._lang()];
  }
  _dayLabel(key) {
    return DAY_LABELS[this._lang()][key] || key;
  }

  _buildDraft(days) {
    return DAYS.map((_, i) => this._buildDayDraft(days, i));
  }

  _buildDayDraft(days, i) {
    const def = DAYS[i];
    const src = (days || []).find((d) => d && d.day === def.num);
    const periods = [];
    if (src && Array.isArray(src.periods)) {
      for (const p of src.periods) {
        const start = p.start_hhmm || this._minToHHMM(p.start_min);
        const end = p.end_hhmm || this._minToHHMM(p.end_min);
        if (!start || !end) continue;
        periods.push({
          start,
          end,
          zones: Array.isArray(p.zone_ids) ? p.zone_ids.slice() : [],
        });
      }
    }
    return {
      num: def.num,
      key: def.key,
      enabled: !!(src && src.enabled),
      periods,
      _dirty: false,
      _saving: false,
      _rev: 0,
      _expanded: false,
    };
  }

  _minToHHMM(min) {
    if (typeof min !== "number" || isNaN(min)) return null;
    const h = Math.floor(min / 60) % 24;
    const m = min % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
  }

  // Snap "HH:MM" to the nearest 15-minute slot (the mower's resolution).
  // A value rounding up to 1440 (24:00) becomes "00:00" — read as end-of-day
  // in the END position (see the end-of-day convention above).
  _snap15(hhmm) {
    const [h, m] = String(hhmm).split(":").map((x) => parseInt(x, 10));
    if (isNaN(h) || isNaN(m)) return hhmm;
    let total = Math.round((h * 60 + m) / 15) * 15;
    // Cap at 23:45 (the last valid start/end slot below midnight). End-of-day
    // (24:00) is expressed by the literal "00:00" end value, which _endMin
    // reads as 1440 — so _snap15 never emits a 1440/"00:00" that would be
    // ambiguous with a real midnight start.
    if (total >= 1440) total = 1425;
    return this._minToHHMM(total);
  }

  _hhmmToMin(hhmm) {
    const [h, m] = String(hhmm).split(":").map((x) => parseInt(x, 10));
    return (isNaN(h) ? 0 : h) * 60 + (isNaN(m) ? 0 : m);
  }

  // End minutes, treating "00:00" as end-of-day (1440).
  _endMin(hhmm) {
    const v = this._hhmmToMin(hhmm);
    return v === 0 ? 1440 : v;
  }

  // How an end time is shown in the summary line ("00:00" -> "24:00").
  _dispEnd(hhmm) {
    return this._hhmmToMin(hhmm) === 0 ? "24:00" : hhmm;
  }

  _deviceId() {
    const ent = this._hass?.entities?.[this._config.entity];
    return ent?.device_id || null;
  }

  _cardTitle() {
    return this._config?.title || this._t().title;
  }

  // ---- rendering -----------------------------------------------------------
  _renderMessage(msg) {
    this.shadowRoot.innerHTML = `
      <ha-card header="${this._escape(this._cardTitle())}">
        <div class="pad">${this._escape(msg)}</div>
      </ha-card>
      ${this._styleTag()}
    `;
    this._rendered = false;
  }

  _render() {
    const rows = this._draft.map((day, di) => this._renderDay(day, di)).join("");
    this.shadowRoot.innerHTML = `
      <ha-card header="${this._escape(this._cardTitle())}">
        <div class="days">${rows}</div>
      </ha-card>
      ${this._styleTag()}
    `;
    this._attachEvents();
    this._rendered = true;
  }

  _renderDay(day, di) {
    const t = this._t();
    const s = this._status[day.key];
    const statusText = s ? s.text : "";
    const statusClass = s ? `status ${s.kind}` : "status";
    const periods = day.periods.map((p, pi) => this._renderPeriod(day, di, p, pi)).join("");
    // Collapsed sub-label: period COUNT (or Off) — the times themselves stay
    // hidden until the row is expanded via the chevron.
    const n = day.periods.length;
    const sub = day.enabled ? (n ? `${n} ${n === 1 ? t.slot : t.slots}` : t.off) : t.off;
    const canSave = day._dirty && !day._saving;
    return `
      <div class="day ${day.enabled ? "on" : "off"} ${day._expanded ? "expanded" : ""}" data-di="${di}">
        <div class="day-head">
          <ha-switch data-act="toggle-day" data-di="${di}"></ha-switch>
          <div class="day-name" data-act="toggle-expand" data-di="${di}">
            <div class="day-title">${this._dayLabel(day.key)}</div>
            <div class="day-sub">${this._escape(sub)}</div>
          </div>
          <span class="${statusClass}">${this._escape(statusText)}</span>
          <ha-icon class="chev" data-act="toggle-expand" data-di="${di}" icon="mdi:chevron-down"></ha-icon>
        </div>
        <div class="day-body" ${day._expanded ? "" : "hidden"}>
          <div class="periods">
            ${periods}
            <button class="add" data-act="add-period" data-di="${di}">+ ${this._escape(t.add)}</button>
          </div>
        </div>
        <div class="day-actions" ${day._dirty ? "" : "hidden"}>
          <button class="save" data-act="save-day" data-di="${di}" ${canSave ? "" : "disabled"}>${this._escape(t.save)}</button>
          <button class="discard" data-act="discard-day" data-di="${di}" ${canSave ? "" : "hidden"}>${this._escape(t.discard)}</button>
        </div>
      </div>
    `;
  }

  _renderPeriod(day, di, p, pi) {
    const t = this._t();
    const zoneChips =
      this._zones.length > 0
        ? `<div class="zones">
             <button class="chip ${p.zones.length === 0 ? "active" : ""}"
                     data-act="zone-all" data-di="${di}" data-pi="${pi}">${this._escape(t.allZones)}</button>
             ${this._zones
               .map(
                 (z) =>
                   `<button class="chip ${p.zones.includes(z.id) ? "active" : ""}"
                            data-act="zone" data-di="${di}" data-pi="${pi}" data-zid="${z.id}">${this._escape(
                     z.name || "Zone " + z.id
                   )}</button>`
               )
               .join("")}
           </div>`
        : "";
    return `
      <div class="period" data-di="${di}" data-pi="${pi}">
        <div class="times">
          <input type="time" step="900" value="${this._escape(p.start)}" data-act="start" data-di="${di}" data-pi="${pi}">
          <span class="arrow">${t.dash}</span>
          <input type="time" step="900" value="${this._escape(p.end)}" data-act="end" data-di="${di}" data-pi="${pi}">
          <button class="del" title="${this._escape(t.remove)}" data-act="del-period" data-di="${di}" data-pi="${pi}">&#10005;</button>
        </div>
        ${zoneChips}
      </div>
    `;
  }

  _touch(day) {
    day._dirty = true;
    day._rev = (day._rev || 0) + 1;
    this._clearStatus(day.key);
  }

  _attachEvents() {
    const root = this.shadowRoot;

    root.querySelectorAll("[data-act='toggle-expand']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        d._expanded = !d._expanded;
        this._render();
      })
    );
    root.querySelectorAll("[data-act='toggle-day']").forEach((el) => {
      const dd = this._draft[+el.dataset.di];
      el.checked = dd.enabled; // native ha-switch initial state
      el.addEventListener("change", (e) => {
        dd.enabled = e.target.checked;
        if (dd.enabled) dd._expanded = true; // open the editor when a day is enabled
        this._touch(dd);
        this._render();
      });
    });
    root.querySelectorAll("[data-act='add-period']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        d.periods.push({ start: "09:00", end: "18:00", zones: [] });
        this._touch(d);
        this._render();
      })
    );
    root.querySelectorAll("[data-act='del-period']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        d.periods.splice(+e.currentTarget.dataset.pi, 1);
        this._touch(d);
        this._render();
      })
    );
    root.querySelectorAll("[data-act='zone']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        const p = d.periods[+e.currentTarget.dataset.pi];
        const zid = +e.currentTarget.dataset.zid;
        const idx = p.zones.indexOf(zid);
        if (idx >= 0) p.zones.splice(idx, 1);
        else p.zones.push(zid);
        this._touch(d);
        this._render();
      })
    );
    root.querySelectorAll("[data-act='zone-all']").forEach((el) =>
      el.addEventListener("click", (e) => {
        const d = this._draft[+e.currentTarget.dataset.di];
        d.periods[+e.currentTarget.dataset.pi].zones = [];
        this._touch(d);
        this._render();
      })
    );

    // Time inputs: update the draft silently (no re-render) so focus/typing is
    // never interrupted; mark dirty and reveal Save/Discard in place.
    root.querySelectorAll("[data-act='start'],[data-act='end']").forEach((el) =>
      el.addEventListener("change", (e) => {
        const d = this._draft[+e.target.dataset.di];
        const p = d.periods[+e.target.dataset.pi];
        const val = e.target.value ? this._snap15(e.target.value) : "";
        if (e.target.dataset.act === "start") p.start = val;
        else p.end = val;
        e.target.value = val;
        this._touch(d);
        this._markDirtyUI(+e.target.dataset.di);
      })
    );

    root.querySelectorAll("[data-act='save-day']").forEach((el) =>
      el.addEventListener("click", (e) => this._saveDay(+e.currentTarget.dataset.di))
    );
    root.querySelectorAll("[data-act='discard-day']").forEach((el) =>
      el.addEventListener("click", (e) => this._discardDay(+e.currentTarget.dataset.di))
    );
  }

  // Reveal Save/Discard + enable Save without a full re-render (keeps input focus).
  _markDirtyUI(di) {
    const dayEl = this.shadowRoot.querySelector(`.day[data-di='${di}']`);
    if (!dayEl) return;
    const actions = dayEl.querySelector(".day-actions");
    if (actions) actions.removeAttribute("hidden");
    const save = dayEl.querySelector("[data-act='save-day']");
    if (save) save.disabled = false;
    const discard = dayEl.querySelector("[data-act='discard-day']");
    if (discard) discard.removeAttribute("hidden");
    const status = dayEl.querySelector(".status");
    if (status) {
      status.textContent = "";
      status.className = "status";
    }
  }

  _clearStatus(key) {
    if (this._clearTimers[key]) {
      clearTimeout(this._clearTimers[key]);
      delete this._clearTimers[key];
    }
    delete this._status[key];
  }

  _discardDay(di) {
    const key = this._draft[di].key;
    const wasExpanded = this._draft[di]._expanded;
    this._draft[di] = this._buildDayDraft(this._serverDays, di);
    this._draft[di]._expanded = wasExpanded; // keep the row open after reverting
    this._clearStatus(key);
    this._render();
  }

  _setStatus(key, kind, text) {
    this._status[key] = { kind, text };
  }

  async _saveDay(di) {
    const t = this._t();
    const day = this._draft[di];
    if (day._saving) return; // re-entrancy guard: one write in flight at a time

    const periods = [];
    for (const p of day.periods) {
      if (!p.start || !p.end) {
        if (day.enabled) {
          this._setStatus(day.key, "error", t.incomplete);
          this._render();
          return;
        }
        continue; // disabled day: silently skip an incomplete row
      }
      const start = this._snap15(p.start);
      const end = this._snap15(p.end);
      if (this._endMin(end) <= this._hhmmToMin(start)) {
        if (day.enabled) {
          this._setStatus(day.key, "error", t.invalid);
          this._render();
          return;
        }
        continue;
      }
      periods.push({ start, end, zones: p.zones.slice() });
    }

    const data = { day: day.key, enabled: day.enabled, periods };
    const deviceId = this._deviceId();
    if (deviceId) data.device_id = deviceId;

    const rev = day._rev;
    day._saving = true;
    this._setStatus(day.key, "saving", t.saving);
    this._render();
    try {
      await this._hass.callService("navimow_pro", "set_schedule", data);
      day._saving = false;
      if (day._rev === rev) {
        // No edit landed while the write was in flight -> this day is now clean.
        day._dirty = false;
        this._setStatus(day.key, "saved", t.saved);
        this._render();
        this._scheduleStatusClear(day.key);
      } else {
        // The user edited during the save; keep it dirty/savable, no 'saved'.
        this._clearStatus(day.key);
        this._render();
      }
    } catch (err) {
      day._saving = false;
      this._setStatus(day.key, "error", t.error);
      // eslint-disable-next-line no-console
      console.error("navimow-scheduler-card: set_schedule failed", err);
      this._render();
    }
  }

  // Auto-clear a 'saved' badge in place after a few seconds (no re-render, so
  // it never steals focus from another day being edited).
  _scheduleStatusClear(key) {
    if (this._clearTimers[key]) clearTimeout(this._clearTimers[key]);
    this._clearTimers[key] = setTimeout(() => {
      delete this._clearTimers[key];
      if (this._status[key] && this._status[key].kind === "saved") {
        delete this._status[key];
        const dayEl = [...this.shadowRoot.querySelectorAll(".day")].find(
          (el) => this._draft[+el.dataset.di] && this._draft[+el.dataset.di].key === key
        );
        const status = dayEl && dayEl.querySelector(".status");
        if (status) {
          status.textContent = "";
          status.className = "status";
        }
      }
    }, 3000);
  }

  _escape(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _styleTag() {
    return `<style>
      :host { display: block; }
      .pad { padding: 16px; color: var(--secondary-text-color); }
      .days { padding: 4px 8px 12px; }
      .day {
        border-bottom: 1px solid var(--divider-color, #e0e0e0);
        padding: 10px 8px;
      }
      .day:last-child { border-bottom: none; }
      .day-head { display: flex; align-items: center; gap: 12px; }
      .day-name { flex: 1; min-width: 0; cursor: pointer; }
      .day-title { font-weight: 500; color: var(--primary-text-color); }
      .chev {
        cursor: pointer; flex: none; color: var(--secondary-text-color);
        --mdc-icon-size: 24px;
        transform: rotate(-90deg); transition: transform 0.15s ease;
      }
      .day.expanded .chev { transform: rotate(0deg); }
      .chev:hover { color: var(--primary-text-color); }
      ha-switch { flex: none; }
      .day-body[hidden] { display: none; }
      .day-sub {
        font-size: 0.8rem; color: var(--secondary-text-color);
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      }
      .day.off .day-title { color: var(--secondary-text-color); }
      .status { font-size: 0.78rem; color: var(--secondary-text-color); min-height: 1em; text-align: right; }
      .status.saved { color: var(--success-color, #43a047); }
      .status.error { color: var(--error-color, #db4437); }
      .status.saving { color: var(--primary-color); }

      .periods { margin: 10px 0 4px 54px; }
      .periods[hidden] { display: none; }
      .period {
        background: var(--secondary-background-color, rgba(0,0,0,0.04));
        border-radius: 10px; padding: 10px; margin-bottom: 8px;
      }
      .times { display: flex; align-items: center; gap: 8px; }
      .times input[type="time"] {
        font-size: 1rem; padding: 6px 8px; border-radius: 8px;
        border: 1px solid var(--divider-color, #ccc);
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color); color-scheme: light dark;
      }
      .arrow { color: var(--secondary-text-color); }
      .del {
        margin-left: auto; border: none; background: transparent;
        color: var(--secondary-text-color); cursor: pointer; font-size: 1rem;
        border-radius: 50%; width: 30px; height: 30px;
      }
      .del:hover { background: var(--divider-color, rgba(0,0,0,0.1)); color: var(--error-color, #db4437); }

      .zones { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
      .chip {
        border: 1px solid var(--primary-color, #03a9f4);
        background: transparent; color: var(--primary-color, #03a9f4);
        border-radius: 16px; padding: 3px 12px; font-size: 0.82rem; cursor: pointer;
      }
      .chip.active { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }

      .add {
        border: 1px dashed var(--primary-color, #03a9f4); background: transparent;
        color: var(--primary-color, #03a9f4); border-radius: 8px;
        padding: 6px 12px; cursor: pointer; font-size: 0.85rem;
      }
      .day-actions { margin: 8px 0 2px 54px; display: flex; gap: 8px; align-items: center; }
      .day-actions[hidden] { display: none; }
      .save {
        background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff);
        border: none; border-radius: 8px; padding: 7px 18px; cursor: pointer; font-size: 0.9rem;
      }
      .save[disabled] { opacity: 0.45; cursor: default; }
      .discard {
        background: transparent; color: var(--secondary-text-color);
        border: 1px solid var(--divider-color, #ccc); border-radius: 8px;
        padding: 7px 14px; cursor: pointer; font-size: 0.9rem;
      }
      .discard[hidden] { display: none; }
    </style>`;
  }
}

if (!customElements.get("navimow-scheduler-card")) {
  customElements.define("navimow-scheduler-card", NavimowSchedulerCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "navimow-scheduler-card",
  name: "Navimow Scheduler",
  description: "Weekly graphical mowing-schedule editor for the Navimow (Private) integration.",
  preview: false,
});
