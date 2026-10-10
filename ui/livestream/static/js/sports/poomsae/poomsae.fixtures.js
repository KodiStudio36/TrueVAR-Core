/**
 * Sample payloads + a fake performance for testing the overlay with the
 * MockSocket (no backend). From the browser console:
 *
 *   perform()                    solo performance
 *   perform(fixtures.duel)       side-by-side match
 *   perform(fixtures.para)       para: no live score / deductions
 *   data(fixtures.solo)          just bind a payload
 */

export const solo = {
    match_id: 'demo-solo',
    id_formatted: '012',
    category: 'Seniors Under 30 A Men',     // already shortened by the backend
    live_score: true,
    phase: 'Preliminary Round',
    stage: 'poomsae_1',
    round_key: 'poomsae1',
    poomsae_index: 1,
    poomsae_name: 'Taeguk 7 Jang',
    mode: 'solo',
    is_solo: true,
    visible: true,
    result: false,
    leader: null,
    clk: '1:30',
    blue: { name: 'Oliver Markus Hlavinka', club: 'TKD Bratislava', flag2: 'sk', flag3: 'SVK', accuracy: 4, display_score: 4, score: 10, deduction: 0 },
    red: null,
};

export const duel = {
    ...solo,
    match_id: 'demo-duel',
    id_formatted: '031',
    category: 'Juniors Male',
    phase: 'Quarter-Final',
    mode: 'duel',
    is_solo: false,
    blue: { name: 'Tomas Seckar', flag2: 'sk', flag3: 'SVK', accuracy: 4, display_score: 4, score: 10, deduction: 0 },
    red: { name: 'Jakub Spitalik', flag2: 'cz', flag3: 'CZE', accuracy: 4, display_score: 4, score: 10, deduction: 0 },
};

export const para = {
    ...solo,
    match_id: 'demo-para',
    id_formatted: '044',
    category: 'Para Seniors P72 Male',
    mode: 'para',
    live_score: false,
};

const clock = (s) => `${s < 0 ? '-' : ''}${Math.floor(Math.abs(s) / 60)}:${String(Math.abs(s) % 60).padStart(2, '0')}`;

function judgeScores(accuracy, judges) {
    return Array.from({ length: judges }, (_, i) => ({
        judge: i + 1,
        accuracy: +(accuracy + (Math.random() - 0.5) * 0.2).toFixed(1),
        presentation: +(4.2 + Math.random() * 1.2).toFixed(1),
        submitted: true,
        trim_accuracy: false,
        trim_presentation: false,
    }));
}

/** Preparation (header only) → intro → poomsae (scores drop, clock folds,
 *  names shorten, overtime) → presentation mode → Results → Final Score. */
export function simulatePerformance(socket, base = solo, { steps = 16, everyMs = 900, judges = 3 } = {}) {
    const data = structuredClone(base);
    data.result = false;
    data.match_id = `${base.match_id}-${Date.now()}`;
    const corners = data.is_solo ? ['blue'] : ['blue', 'red'];
    corners.forEach((c) => Object.assign(data[c], { accuracy: 4, display_score: 4, score: 10, deduction: 0 }));

    const state = {
        visible: true, header_only: true, compact: false, clock_hidden: false,
        presentation: false, show_results: false, show_final: false
    };
    const push = () => socket.trigger('listener_update', { event: 'update', data: structuredClone(data) });
    const sync = () => socket.trigger('scoreboard_sync', {
        event: 'update',
        data: { result: data.result, show_poomsae_name: state.visible && !state.header_only && !!data.poomsae_name, ...state },
    });
    const tick = (secs) => {
        data.clk = clock(secs);   // the payload carries the clock too, like the real backend
        socket.trigger('clock_update', { event: 'update', data: { clk: data.clk } });
    };
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));

    return (async () => {
        let secs = 90;
        push(); tick(secs); sync();                                   // Preparation: header only
        await wait(2500);

        state.visible = false; sync();                                // Poomsae 1: intro first…
        socket.trigger('athlete_intro', {
            event: 'update', data: {
                is_solo: data.is_solo, phase: data.phase,
                athletes: corners.map((k) => ({ corner: k, ...data[k] })),
            }
        });
        socket.trigger('show_athlete_intro');
        await wait(4000);
        socket.trigger('hide_athlete_intro');
        await wait(400);
        Object.assign(state, { visible: true, header_only: false }); sync();   // …then the scoreboard

        for (let i = 1; i <= steps; i += 1) {
            await wait(everyMs);
            secs -= 6; tick(secs);                                    // runs into overtime near the end
            if (i === 2) { state.clock_hidden = true; sync(); }       // timer ran 5 s → clock slides up
            if (i === 8) { state.compact = true; sync(); }            // 8 s after the rows appeared
            if (secs < 0 && state.clock_hidden) { state.clock_hidden = false; sync(); }   // overtime → clock back, red

            const c = corners[Math.floor(Math.random() * corners.length)];
            const amount = Math.random() < 0.7 ? 0.1 : 0.3;
            const agreeing = Math.random() < 0.5 ? 2 : 1;
            data[c].accuracy = Math.max(0, +(data[c].accuracy - (amount * agreeing) / judges).toFixed(3));
            data[c].display_score = data[c].accuracy;
            push();
            if (agreeing >= 2) socket.trigger('deduction_flash', { event: 'update', data: { corner: c, amount } });
        }

        await wait(1000);
        Object.assign(state, { presentation: true, clock_hidden: true }); sync();   // clock stopped, judges on presentation
        await wait(3500);

        const p1 = {};
        corners.forEach((k) => {                                       // Results 1
            const js = judgeScores(data[k].accuracy, judges);
            const avg = (f) => js.reduce((t, j) => t + j[f], 0) / js.length;
            Object.assign(data[k], { judge_scores: js, accuracy: avg('accuracy'), presentation: avg('presentation') });
            data[k].score = +(data[k].accuracy + data[k].presentation).toFixed(2);
            data[k].display_score = data[k].score;
            p1[k] = data[k].score;
        });
        data.stage = 'results_1';
        data.result = true;
        data.leader = data.is_solo || data.blue.score >= data.red.score ? 'blue' : 'red';
        Object.assign(state, {
            visible: false, header_only: true, compact: false, clock_hidden: false,
            presentation: false, show_results: true
        });
        push(); sync();
        await wait(6000);

        data.stage = 'final_results';                                  // Final Results (2nd poomsae skipped here)
        data.result = false;
        const athletes = corners.map((k) => {
            const p2 = +(p1[k] + (Math.random() - 0.5) * 0.6).toFixed(2);
            return {
                corner: k, name: data[k].name, flag2: data[k].flag2, flag3: data[k].flag3,
                poomsae: [p1[k], p2], total: +((p1[k] + p2) / 2).toFixed(3)
            };
        });
        data.final = {
            columns: [{ label: 'P1', name: data.poomsae_name || 'Poomsae 1' }, { label: 'P2', name: 'Koryo' }],
            athletes,
            leader: athletes.reduce((a, b) => (b.total > a.total ? b : a)).corner,
        };
        data.final_visible = true;
        Object.assign(state, { show_results: false, show_final: true });
        push(); sync();
    })();
}

/** Category Results with n athletes — category(3) sits at the bottom,
 *  category(10) is centred. */
export function showCategory(socket, n = 3) {
    const names = ['Mock Athlete', 'Mock Performer', 'Mock Hungarian', 'Anna-Maria Nová', 'Oliver Markus Hlavinka',
        'Jakub Spitalik', 'Tomas Seckar', 'Park Taejoon', 'Lukas Novak', 'Eva Horvathova', 'Marek Kral', 'Sofia Rossi'];
    const countries = [['sk', 'SVK'], ['ua', 'UKR'], ['hu', 'HUN'], ['cz', 'CZE'], ['sk', 'SVK'], ['cz', 'CZE'],
    ['sk', 'SVK'], ['kr', 'KOR'], ['cz', 'CZE'], ['sk', 'SVK'], ['pl', 'POL'], ['it', 'ITA']];
    const rows = Array.from({ length: n }, (_, i) => ({
        rank: i + 1,
        name: names[i % names.length],
        flag2: countries[i % countries.length][0],
        flag3: countries[i % countries.length][1],
        score: +(9.4 - i * 0.37).toFixed(2),
        qualified: i < Math.min(8, Math.ceil(n / 2)),
        current: i === 1,
    }));
    socket.trigger('listener_update', {
        event: 'update', data: {
            match_id: `demo-cat-${n}`, stage: 'category_results', category: 'Seniors Under 30 A Men',
            category_results: { rows },
        }
    });
    socket.trigger('scoreboard_sync', { event: 'update', data: { visible: false, show_category: true } });
}