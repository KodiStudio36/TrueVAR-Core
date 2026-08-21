/**
 * Console-driven testing. No UI, no panel — just call these from
 * devtools while you're eyeballing/tweaking CSS.
 *
 *   show('winner')          trigger that widget's enter animation
 *   hide('winner')          trigger its exit animation
 *   list()                  print every widget id you can use above
 *   data(fixtures.match)    push sample data into every widget that binds it
 *   demo()                  play every widget's animation back to back
 *   ticker('some text')     show the news ticker
 */
export function attachConsoleTools({ registry, socket, ticker, subscribeCard }, fixtures = {}) {
    window.list = () => {
        console.log([...registry.widgets.keys()].join('\n'));
    };

    window.show = (id) => {
        const w = registry.get(id);
        if (!w) return console.warn(`No widget "${id}". Try list().`);
        w.events?.show ? socket.trigger(w.events.show) : w.controller.show();
    };

    window.hide = (id) => {
        const w = registry.get(id);
        if (!w) return console.warn(`No widget "${id}". Try list().`);
        w.events?.hide ? socket.trigger(w.events.hide) : w.controller.hide();
    };

    window.data = (payload) => {
        registry.updateAll(payload);
    };

    window.ticker = (message = 'TEST MESSAGE', rounds = 3) => ticker.start(message, rounds);
    window.hideTicker = () => ticker.stop();

    window.yt = (show = true) => subscribeCard?.toggle(show);

    window.reset = () => registry.hideAll();

    window.demo = async () => {
        const wait = (ms) => new Promise((r) => setTimeout(r, ms));
        registry.hideAll();
        await wait(500);
        for (const id of registry.widgets.keys()) {
            console.log(`→ ${id}`);
            window.show(id);
            await wait(2400);
            window.hide(id);
            await wait(500);
        }
    };

    window.compact = (id, val) => {
        const w = registry.get(id);
        if (!w) return console.warn(`No widget "${id}".`);
        w.el.classList.toggle('is-compact', val ?? !w.el.classList.contains('is-compact'));
    };

    window.toggleBreak = (isBreak) => {
        socket.trigger(!isBreak ? 'hide_break' : 'show_break');
    };

    console.log(
        '%cOverlay console tools ready.',
        'color:#0b6b67;font-weight:bold;',
        '\nTry: list()  show("winner")  hide("winner")  data(fixtures.match)  demo()'
    );
    window.fixtures = fixtures;
}