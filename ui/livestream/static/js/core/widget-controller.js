/**
 * WidgetController
 * One class, one instance per widget, used by every sport.
 * Replaces the old pattern of hand-writing showX()/hideX() with a
 * busy flag and a magic setTimeout for each individual widget.
 *
 * The element must already carry:
 *   - class "is-hidden" when not shown
 *   - a data-anim="corner|reveal|bars|pop" attribute (see animations.css)
 *
 * Timings default to what the animation itself declares via
 * data-enter-ms / data-exit-ms on the element, falling back to
 * sane defaults, so CSS and JS can't drift out of sync.
 */
export class WidgetController {
    constructor(el, { enterMs, exitMs } = {}) {
        if (!el) throw new Error('WidgetController: element is required');
        this.el = el;
        this.enterMs = enterMs ?? Number(el.dataset.enterMs) ?? 1200;
        this.exitMs = exitMs ?? Number(el.dataset.exitMs) ?? 400;
        this.busy = false;
        this._timer = null;
    }

    get isHidden() {
        return this.el.classList.contains('is-hidden');
    }

    show() {
        if (this.busy || !this.isHidden) return;
        this.busy = true;

        this.el.classList.remove('is-hidden', 'is-exiting');
        this.el.classList.add('is-animating', 'is-entering');

        // Force reflow so re-triggering the animation from a
        // rapid show/hide/show works reliably.
        void this.el.offsetWidth;

        clearTimeout(this._timer);
        this._timer = setTimeout(() => {
            this.el.classList.remove('is-entering', 'is-animating');
            this.busy = false;
        }, this.enterMs);
    }

    hide() {
        if (this.busy || this.isHidden) return;
        this.busy = true;

        this.el.classList.remove('is-entering');
        this.el.classList.add('is-animating', 'is-exiting');

        clearTimeout(this._timer);
        this._timer = setTimeout(() => {
            this.el.classList.remove('is-exiting', 'is-animating');
            this.el.classList.add('is-hidden');
            this.busy = false;
        }, this.exitMs);
    }

    toggle(show) {
        show ? this.show() : this.hide();
    }
}

/**
 * Simple transition-based widget (no enter/exit choreography,
 * just a hidden <-> shown CSS transition). Used for things like
 * the subscribe card, where a busy-lock/animation sequence is
 * overkill.
 */
export class SimpleToggle {
    constructor(el) {
        this.el = el;
    }
    show() { this.el.classList.remove('is-hidden'); }
    hide() { this.el.classList.add('is-hidden'); }
    toggle(show) { show ? this.show() : this.hide(); }
}