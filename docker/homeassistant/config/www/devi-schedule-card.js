const DAY_NAMES = [
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"],
];

const SLOT_COUNT = 48;
const LABELS = {
  en: {
    reload: "Reload",
    save: "Save",
    saving: "Saving...",
    saved: "Saved",
    saveFailed: "Save failed",
    weeklySchedule: "Weekly Schedule",
    scheduleUnavailable: "Schedule entities are not available yet.",
    off: "Off",
    dayNames: {
      mon: "Mon",
      tue: "Tue",
      wed: "Wed",
      thu: "Thu",
      fri: "Fri",
      sat: "Sat",
      sun: "Sun",
    },
  },
  pl: {
    reload: "Odśwież",
    save: "Zapisz",
    saving: "Zapisywanie...",
    saved: "Zapisano",
    saveFailed: "Zapis nie powiódł się",
    weeklySchedule: "Harmonogram tygodniowy",
    scheduleUnavailable: "Encje harmonogramu nie są jeszcze dostępne.",
    off: "Wyłączone",
    dayNames: {
      mon: "Pon",
      tue: "Wt",
      wed: "Śr",
      thu: "Czw",
      fri: "Pt",
      sat: "Sob",
      sun: "Nd",
    },
  },
};

function parseRanges(value) {
  const slots = Array(SLOT_COUNT).fill(false);
  if (!value || value.trim() === "") {
    return slots;
  }
  for (const rawRange of value.split(",")) {
    const range = rawRange.trim();
    if (!range) continue;
    const [start, end] = range.split("-").map((part) => part.trim());
    const startIndex = timeToIndex(start);
    const endIndex = timeToIndex(end);
    for (let i = startIndex; i < endIndex; i += 1) {
      slots[i] = true;
    }
  }
  return slots;
}

function timeToIndex(value) {
  const [hourText, minuteText] = value.split(":");
  return Number(hourText) * 2 + (Number(minuteText) >= 30 ? 1 : 0);
}

function indexToTime(index) {
  const hour = Math.floor(index / 2);
  const minute = index % 2 === 0 ? "00" : "30";
  return `${String(hour).padStart(2, "0")}:${minute}`;
}

function slotsToRanges(slots) {
  const ranges = [];
  let start = null;
  for (let i = 0; i <= SLOT_COUNT; i += 1) {
    const active = i < SLOT_COUNT ? slots[i] : false;
    if (active && start === null) {
      start = i;
      continue;
    }
    if (!active && start !== null) {
      ranges.push(`${indexToTime(start)}-${indexToTime(i)}`);
      start = null;
    }
  }
  return ranges;
}

function cloneDays(days) {
  return Object.fromEntries(Object.entries(days).map(([key, value]) => [key, [...value]]));
}

class DeviScheduleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    if (!config.climate_entity) {
      throw new Error("climate_entity is required");
    }
    this._config = {
      title: config.title || "Weekly Schedule",
      climate_entity: config.climate_entity,
    };
  }

  set hass(hass) {
    this._hass = hass;
    this._syncFromStates();
    this._render();
  }

  getCardSize() {
    return 8;
  }

  _labels() {
    const lang = this._hass?.locale?.language?.startsWith("pl") ? "pl" : "en";
    return LABELS[lang];
  }

  _entityIdForDay(dayKey) {
    const objectId = this._config.climate_entity.split(".")[1];
    return `text.${objectId}_schedule_${dayKey}`;
  }

  _syncFromStates() {
    if (!this._hass || !this._config) return;
    const nextDays = {};
    let allPresent = true;
    const climateState = this._hass.states[this._config.climate_entity]?.state;
    const climateUnavailable = climateState === "unavailable" || climateState === "unknown";
    for (const [dayKey] of DAY_NAMES) {
      const entityId = this._entityIdForDay(dayKey);
      const state = this._hass.states[entityId];
      if (!state) {
        allPresent = false;
        break;
      }
      if (climateUnavailable && (!state.state || state.state === "unknown" || state.state === "unavailable")) {
        allPresent = false;
        break;
      }
      nextDays[dayKey] = parseRanges(state.state);
    }
    if (!allPresent) {
      this._available = false;
      return;
    }
    this._available = true;
    const nextSignature = JSON.stringify(nextDays);
    if (!this._dirty || this._serverSignature !== nextSignature) {
      this._serverDays = nextDays;
      if (!this._dirty) {
        this._draftDays = cloneDays(nextDays);
      }
      this._serverSignature = nextSignature;
    }
  }

  _toggleSlot(dayKey, slotIndex) {
    if (!this._draftDays) return;
    this._draftDays[dayKey][slotIndex] = !this._draftDays[dayKey][slotIndex];
    this._dirty = true;
    this._message = "";
    this._render();
  }

  async _reload() {
    if (!this._serverDays) return;
    this._draftDays = cloneDays(this._serverDays);
    this._dirty = false;
    this._message = "";
    this._render();
  }

  async _save() {
    if (!this._hass || !this._draftDays) return;
    this._saving = true;
    this._message = "";
    this._render();
    try {
      for (const [dayKey] of DAY_NAMES) {
        const entityId = this._entityIdForDay(dayKey);
        const value = slotsToRanges(this._draftDays[dayKey]).join(", ");
        await this._hass.callService("text", "set_value", {
          entity_id: entityId,
          value,
        });
      }
      this._dirty = false;
      this._message = this._labels().saved;
      this._serverDays = cloneDays(this._draftDays);
      this._serverSignature = JSON.stringify(this._serverDays);
    } catch (error) {
      this._message = error?.message || this._labels().saveFailed;
    } finally {
      this._saving = false;
      this._render();
    }
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const labels = this._labels();
    const hourLabels = Array.from({ length: 24 }, (_, hour) => {
      if (hour % 2 !== 0) return `<div class="hour hour--empty"></div>`;
      return `<div class="hour">${String(hour).padStart(2, "0")}</div>`;
    }).join("");

    const rows = this._available && this._draftDays
      ? DAY_NAMES.map(([dayKey]) => {
          const dayLabel = labels.dayNames[dayKey];
          const cells = this._draftDays[dayKey]
            .map((active, index) => `
              <button
                class="slot ${active ? "slot--active" : ""}"
                data-day="${dayKey}"
                data-slot="${index}"
                title="${dayLabel} ${indexToTime(index)}-${indexToTime(index + 1)}"
              ></button>
            `)
            .join("");
          const summary = slotsToRanges(this._draftDays[dayKey]).join(", ") || labels.off;
          return `
            <div class="row">
              <div class="day">${dayLabel}</div>
              <div class="grid">${cells}</div>
              <div class="summary">${summary}</div>
            </div>
          `;
        }).join("")
      : `<div class="placeholder">${labels.scheduleUnavailable}</div>`;

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
        }
        ha-card {
          padding: 16px;
        }
        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 12px;
        }
        .title {
          font-size: 1.1rem;
          font-weight: 600;
        }
        .actions {
          display: flex;
          gap: 8px;
        }
        button.action {
          border: 0;
          border-radius: 999px;
          padding: 8px 12px;
          font: inherit;
          cursor: pointer;
          background: var(--secondary-background-color);
          color: var(--primary-text-color);
        }
        button.action--primary {
          background: var(--primary-color);
          color: var(--text-primary-color, white);
        }
        button.action[disabled] {
          opacity: 0.5;
          cursor: default;
        }
        .legend {
          display: grid;
          grid-template-columns: 72px repeat(24, minmax(10px, 1fr));
          gap: 4px;
          margin-bottom: 8px;
          align-items: center;
          color: var(--secondary-text-color);
          font-size: 0.75rem;
        }
        .hour {
          text-align: center;
        }
        .hour--empty {
          visibility: hidden;
        }
        .row {
          display: grid;
          grid-template-columns: 72px minmax(0, 1fr);
          gap: 8px;
          align-items: start;
          margin-bottom: 12px;
        }
        .day {
          font-weight: 600;
          padding-top: 4px;
        }
        .grid {
          display: grid;
          grid-template-columns: repeat(48, minmax(6px, 1fr));
          gap: 2px;
        }
        .slot {
          display: block;
          width: 100%;
          aspect-ratio: 1 / 1.8;
          border: 0;
          border-radius: 2px;
          padding: 0;
          cursor: pointer;
          background: color-mix(in srgb, var(--divider-color) 70%, transparent);
        }
        .slot--active {
          background: var(--primary-color);
        }
        .summary {
          grid-column: 2;
          font-size: 0.8rem;
          color: var(--secondary-text-color);
          text-align: left;
          min-height: 1rem;
        }
        .message {
          margin-top: 8px;
          font-size: 0.8rem;
          color: var(--secondary-text-color);
        }
        .placeholder {
          color: var(--secondary-text-color);
        }
        @media (max-width: 900px) {
          .legend {
            grid-template-columns: 56px repeat(24, minmax(8px, 1fr));
          }
          .row {
            grid-template-columns: 56px minmax(0, 1fr);
          }
        }
      </style>
      <ha-card>
        <div class="header">
          <div class="title">${this._config.title || labels.weeklySchedule}</div>
          <div class="actions">
            <button class="action" id="reload">${labels.reload}</button>
            <button class="action action--primary" id="save" ${!this._dirty || this._saving ? "disabled" : ""}>
              ${this._saving ? labels.saving : labels.save}
            </button>
          </div>
        </div>
        <div class="legend">
          <div></div>
          ${hourLabels}
        </div>
        ${rows}
        <div class="message">${this._message || ""}</div>
      </ha-card>
    `;

    this.shadowRoot.querySelectorAll(".slot").forEach((button) => {
      button.addEventListener("click", () => {
        const day = button.dataset.day;
        const slot = Number(button.dataset.slot);
        this._toggleSlot(day, slot);
      });
    });
    this.shadowRoot.getElementById("reload")?.addEventListener("click", () => this._reload());
    this.shadowRoot.getElementById("save")?.addEventListener("click", () => this._save());
  }
}

customElements.define("devi-schedule-card", DeviScheduleCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "devi-schedule-card",
  name: "DEVI Schedule Card",
  description: "Weekly grid editor for DEVI thermostat schedules.",
});
