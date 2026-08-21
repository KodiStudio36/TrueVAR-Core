/**
 * News-style ticker. Universal broadcast chrome — not sport-specific,
 * so it's wired directly in app.js rather than through a sport plugin.
 */
export class TickerController {
    constructor(rootSelector = '#widget-ticker', wrapperSelector = '#ticker-wrapper', textSelector = '#ticker-msg') {
        this.root = document.querySelector(rootSelector);
        this.wrapper = document.querySelector(wrapperSelector);
        this.text = document.querySelector(textSelector);
        this.count = 0;
        this.maxRounds = 3;
    }

    start(message, rounds = 3) {
        if (!this.root) return;
        this.text.textContent = message;
        this.count = 0;
        this.maxRounds = rounds;

        this.root.classList.add('active');

        setTimeout(() => this.wrapper.classList.add('scrolling'), 500);

        this.wrapper.onanimationiteration = () => this._tick();
        this.wrapper.onanimationend = () => this._tick(true);
    }

    _tick(isEnd = false) {
        this.count += 1;
        if (this.count >= this.maxRounds) {
            this.stop();
        } else if (isEnd) {
            this.wrapper.classList.remove('scrolling');
            void this.wrapper.offsetWidth;
            this.wrapper.classList.add('scrolling');
        }
    }

    stop() {
        if (!this.root) return;
        this.root.classList.remove('active');
        setTimeout(() => this.wrapper.classList.remove('scrolling'), 500);
    }
}