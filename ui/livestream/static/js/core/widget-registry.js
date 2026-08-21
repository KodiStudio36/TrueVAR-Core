import { WidgetController, SimpleToggle } from './widget-controller.js';
import { bindData } from './data-binder.js';

/**
 * @typedef {Object} WidgetDef
 * @property {string} id                 Unique id, e.g. "round-results"
 * @property {string} selector           CSS selector for the widget root
 * @property {'animated'|'simple'} [kind] Controller type. Default 'animated'.
 * @property {number} [enterMs]          Override enter duration
 * @property {number} [exitMs]           Override exit duration
 * @property {{show?:string,hide?:string}} [events]  Socket event names
 * @property {Object<string, {on?:string, off?:string}>} [stateEvents] Maps CSS classes 
 *                                       to socket events
 * @property {boolean} [bindsData]       Set false if the widget never binds data
 * @property {(data:object)=>object} [prepareData]  Reshape raw payload before binding
 *                                        (e.g. pick the current round out of an array)
 * @property {(el:HTMLElement,data:object,prepared:object)=>void} [onUpdate]
 *                                        Escape hatch for anything too custom for
 *                                        declarative data-bind attributes
 */

export class WidgetRegistry {
    constructor() {
        /** @type {Map<string, WidgetDef & {el:HTMLElement, controller:WidgetController|SimpleToggle}>} */
        this.widgets = new Map();
    }

    /** @param {WidgetDef} def */
    register(def) {
        const el = document.querySelector(def.selector);
        if (!el) {
            console.warn(`[widget-registry] "${def.id}" not found for selector "${def.selector}" — skipped`);
            return;
        }
        const controller = def.kind === 'simple'
            ? new SimpleToggle(el)
            : new WidgetController(el, { enterMs: def.enterMs, exitMs: def.exitMs });

        this.widgets.set(def.id, { ...def, el, controller });
    }

    registerAll(defs) {
        defs.forEach((def) => this.register(def));
    }

    get(id) {
        return this.widgets.get(id);
    }

    /** Push a fresh data payload into every widget that binds data. */
    updateAll(data) {
        this.widgets.forEach((w) => {
            if (w.bindsData === false) return;
            const prepared = w.prepareData ? w.prepareData(data) : data;
            bindData(w.el, prepared);
            if (w.onUpdate) w.onUpdate(w.el, data, prepared);
        });
    }

    hideAll() {
        this.widgets.forEach((w) => w.controller.hide());
    }
}