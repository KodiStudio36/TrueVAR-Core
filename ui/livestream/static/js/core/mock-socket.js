/**
 * MockSocket
 * Implements just enough of the socket.io client API (on, onAny, off,
 * emit) for socket-manager.js to work against it unmodified. A test
 * harness calls .trigger(event, payload) wherever a real server would
 * have emitted — so the exact same wiring code (app.js, plugins) runs
 * in both production and the test harness.
 */
export class MockSocket {
    constructor() {
        this.handlers = new Map();
        this.anyHandlers = [];
    }

    on(event, cb) {
        if (!this.handlers.has(event)) this.handlers.set(event, []);
        this.handlers.get(event).push(cb);
        return this;
    }

    onAny(cb) {
        this.anyHandlers.push(cb);
        return this;
    }

    off(event, cb) {
        if (!this.handlers.has(event)) return this;
        if (!cb) { this.handlers.delete(event); return this; }
        this.handlers.set(event, this.handlers.get(event).filter((fn) => fn !== cb));
        return this;
    }

    /** Simulates a server pushing an event down to the client. */
    trigger(event, payload) {
        this.anyHandlers.forEach((fn) => fn(event, payload));
        (this.handlers.get(event) || []).forEach((fn) => fn(payload));
    }

    /** Real client code calling socket.emit() just loops back locally. */
    emit(event, payload) {
        this.trigger(event, payload);
    }
}