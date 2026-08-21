import { WidgetRegistry } from './widget-registry.js';
import { wireSocket } from './socket-manager.js';
import { TickerController } from './ticker-controller.js';
import { SimpleToggle } from './widget-controller.js';

/**
 * Boots the overlay for a given sport plugin.
 *
 * @param {Object} sportPlugin  Exposes `widgets: WidgetDef[]`
 * @param {Object} [opts]
 * @param {string} [opts.socketUrl]
 */
export function bootOverlay(sportPlugin, opts = {}) {
    const {
        socketUrl = 'http://localhost:8000/truevar',
        socket: injectedSocket = null,
    } = opts;

    const registry = new WidgetRegistry();
    registry.registerAll(sportPlugin.widgets);

    // Universal chrome that every sport gets for free.
    const ticker = new TickerController();
    const subscribeCard = document.querySelector('#widget-yt')
        ? new SimpleToggle(document.querySelector('#widget-yt'))
        : null;

    // Pass a mock socket (see js/core/mock-socket.js) to run the whole
    // overlay + control panel with no backend at all — see test/harness.html.
    const socket = injectedSocket ?? io(socketUrl);
    wireSocket(socket, registry);

    socket.on('show_ticker', (data) => ticker.start(data.message, 3));
    socket.on('hide_ticker', () => ticker.stop());
    if (subscribeCard) {
        socket.on('show_yt', () => subscribeCard.show());
        socket.on('hide_yt', () => subscribeCard.hide());
    }

    // Handy for debugging from the browser console.
    window.__overlay = { registry, socket, ticker, subscribeCard };

    return { registry, socket, ticker, subscribeCard };
}