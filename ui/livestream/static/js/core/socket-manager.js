/**
 * Wires a socket.io connection to a WidgetRegistry generically.
 * Sports never call socket.on() themselves — they declare event
 * names in their widget defs and this does the rest.
 */
export function wireSocket(socket, registry, opts = {}) {
    const {
        updateEvent = 'listener_update',
        resetEvent = 'reset_widgets',
        logTraffic = true,
    } = opts;

    if (logTraffic) {
        socket.onAny((eventName, ...args) => console.log(`[socket] ${eventName}`, args));
    }
    socket.on('connect', () => console.log('[socket] connected', socket.id));
    socket.on('connect_error', (err) => console.error('[socket] connect_error:', err.message));
    socket.on('disconnect', (reason) => console.warn('[socket] disconnected:', reason));

    registry.widgets.forEach((w) => {
        if (w.events?.show) socket.on(w.events.show, () => w.controller.show());
        if (w.events?.hide) socket.on(w.events.hide, () => w.controller.hide());

        if (w.stateEvents) {
            Object.entries(w.stateEvents).forEach(([cssClass, triggers]) => {
                if (triggers.on) socket.on(triggers.on, () => w.el.classList.add(cssClass));
                if (triggers.off) socket.on(triggers.off, () => w.el.classList.remove(cssClass));
            });
        }

        if (w.customEvents) {
            Object.entries(w.customEvents).forEach(([eventName, handler]) => {
                socket.on(eventName, (payload) => handler({ ...w, registry }, payload));
            });
        }
    });

    socket.on(updateEvent, (payload) => {
        if (payload?.event === 'update' && payload.data) registry.updateAll(payload.data);
    });

    socket.on(resetEvent, (payload) => {
        if (payload?.event !== 'reset') return;
        if (!payload.data) {
            registry.hideAll();
        } else {
            payload.data.forEach((id) => registry.get(id)?.controller.hide());
        }
    });

    return socket;
}