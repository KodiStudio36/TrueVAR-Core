import { formatters } from '../../core/data-binder.js';

const SCORE_TYPES = {
    '1': { label: 'PUNCH' },
    '2': { label: 'TRUNK' },
    '3': { label: 'HEAD' },
};
const EVENT_FLASH_MS = 3000;

function flashEventBox(container, pType, durationMs = EVENT_FLASH_MS) {
    const info = SCORE_TYPES[String(pType)];
    if (!container || !info) return;

    const box = document.createElement('div');
    box.className = `event-box bg-green`;
    box.textContent = info.label;
    container.appendChild(box);

    setTimeout(() => box.remove(), durationMs);
}

function renderScoreBoxes(container, points, opponentPoints, currentRound) {
    if (!container) return;
    const activeFlashes = [...container.querySelectorAll('.event-box')];
    container.innerHTML = '';

    const rounds = Array.isArray(points) ? points.slice(0, currentRound) : [];
    const opponentRounds = Array.isArray(opponentPoints) ? opponentPoints.slice(0, currentRound) : [];

    rounds.forEach((round, i) => {
        const opponentScore = opponentRounds[i]?.points ?? 0;
        const isRoundWinner = (round.points ?? 0) > opponentScore && i != currentRound - 1;
        const box = document.createElement('div');
        box.className = isRoundWinner ? 'point-box winner' : 'point-box';
        box.innerHTML = `<i class="main">${round.points ?? 0}</i>`;
        container.appendChild(box);
    });

    if (rounds[currentRound - 1]?.penalties > 0) {
        const penaltyBox = document.createElement('div');
        penaltyBox.className = 'point-box penalty';
        penaltyBox.innerHTML = `<i class="main">${rounds[currentRound - 1].penalties}</i>`;
        container.appendChild(penaltyBox);
    }

    activeFlashes.forEach((box) => container.appendChild(box));
}

function renderPreviousResults(el, data) {
    if (!data) return;
    const { corner = 'blue', athlete_name = '', athlete_flag2 = 'un', athlete_flag3 = '', fights = [] } = data;

    el.querySelector('.color-bar')?.classList.remove('bg-blue', 'bg-red');
    el.querySelector('.color-bar')?.classList.add(corner === 'red' ? 'bg-red' : 'bg-blue');

    const flagImg = el.querySelector('.flag-lg');
    if (flagImg) flagImg.src = `https://flagcdn.com/w80/${athlete_flag2}.png`;

    const countryEl = el.querySelector('.header-country');
    if (countryEl) countryEl.textContent = athlete_flag3.toUpperCase();

    const nameEl = el.querySelector('.header-name');
    if (nameEl) nameEl.textContent = athlete_name.toUpperCase();

    const container = el.querySelector('.results-container');
    if (!container) return;
    container.innerHTML = '';

    fights.forEach((fight) => {
        const row = document.createElement('div');
        row.className = 'row-wrapper';
        row.innerHTML = `
            <div class="row-content grid-result">
                <div class="res-country">${(fight.opponent_flag3 || '').toUpperCase()}</div>
                <img class="flag-sm" src="https://flagcdn.com/w40/${fight.opponent_flag2 || 'un'}.png" alt="">
                <div class="res-name">${fight.opponent_name}</div>
                <div class="res-round">${fight.round_label}</div>
                <div class="res-score">${fight.win_score}</div>
                <div class="res-outcome">${fight.match_id ?? ''}</div>
            </div>`;
        container.appendChild(row);
    });
}

export const taekwondoPlugin = {
    widgets: [
        {
            id: 'next-round',
            selector: '#widget-next-round',
            events: { show: 'show_next_round', hide: 'hide_next_round' },
            prepareData: (data) => ({
                ...data,
                id_formatted: formatters.matchId(data.match_id ?? data.id),
            }),
        },
        {
            id: 'scoreboard',
            selector: '#widget-scoreboard-system',
            events: { show: 'show_scoreboard', hide: 'hide_scoreboard' },
            stateEvents: {
                'state-break': { on: 'show_break', off: 'hide_break' },
                'is-compact': { on: 'compact_scoreboard', off: 'uncompact_scoreboard' }
            },
            prepareData: (data) => ({
                ...data,
                id_formatted: formatters.matchId(data.match_id ?? data.id),
            }),
            onUpdate: (el, data, prepared) => {
                const round = prepared.round ?? 1;
                renderScoreBoxes(el.querySelector('#blue-dynamic-scores'), prepared.blue?.points, prepared.red?.points, round);
                renderScoreBoxes(el.querySelector('#red-dynamic-scores'), prepared.red?.points, prepared.blue?.points, round);

                // doctor flag -> clock chip turns the same yellow as a penalty box
                const els = el.querySelectorAll('[data-bind="clk"]');
                els.forEach(el => el.classList.toggle('is-medical', !!prepared.doctor));
            },
            customEvents: {
                clock_update: (widget, payload) => {
                    if (payload?.data?.clk !== undefined) {
                        const els = widget.el.querySelectorAll('[data-bind="clk"]');
                        els.forEach(el => el.textContent = payload.data.clk);
                    }
                },
                blue_scored: (widget, payload) => {
                    flashEventBox(widget.el.querySelector('#blue-dynamic-scores'), payload?.data?.p_type);
                },
                red_scored: (widget, payload) => {
                    flashEventBox(widget.el.querySelector('#red-dynamic-scores'), payload?.data?.p_type);
                },
            },
        },
        {
            id: 'fighter-bars',
            selector: '#widget-fighter-bars',
            events: { show: 'show_fighter_bars', hide: 'hide_fighter_bars' },
        },
        {
            id: 'previous-results',
            selector: '#widget-previous-results',
            events: { show: 'show_previous_results', hide: 'hide_previous_results' },
            bindsData: false,
            customEvents: {
                previous_results_update: (widget, payload) => renderPreviousResults(widget.el, payload?.data),
            },
        },
        {
            id: 'round-results',
            selector: '#widget-round-results',
            events: { show: 'show_round_results', hide: 'hide_round_results' },
            prepareData: (data) => {
                const round = data.round ?? 1;
                const roundIndex = Math.max(0, round - 1);

                const rawBlue = data.blue || {};
                const rawRed = data.red || {};

                // Pick the point breakdown object for the current active round
                const blueRoundStats = Array.isArray(rawBlue.points) ? (rawBlue.points[roundIndex] || {}) : {};
                const redRoundStats = Array.isArray(rawRed.points) ? (rawRed.points[roundIndex] || {}) : {};

                return {
                    round,
                    category: data.category,
                    title: data.title,
                    // Merging fighter info with the round-specific point breakdown
                    blue: {
                        name: rawBlue.name,
                        flag2: rawBlue.flag2,
                        flag3: rawBlue.flag3,
                        ...blueRoundStats, // provides blue.points, blue.hits, blue.trunk, blue.rotation_trunk, etc.
                    },
                    red: {
                        name: rawRed.name,
                        flag2: rawRed.flag2,
                        flag3: rawRed.flag3,
                        ...redRoundStats, // provides red.points, red.hits, red.trunk, red.rotation_trunk, etc.
                    },
                };
            },
        },
        {
            id: 'winner',
            selector: '#widget-winner',
            events: { show: 'show_win', hide: 'hide_win' },
            prepareData: (data) => {
                const matchId = data.match_id ?? data.id;
                const id_formatted = formatters.matchId(matchId);

                // Pick winner based on "win": "blue" or "red"
                const winnerKey = data.win;
                const winner = data[winnerKey] || {};

                return {
                    ...data,
                    id_formatted,
                    header_line: `${id_formatted} ${data.category ? formatters.category(data.category) : ''}`.trim(),
                    win_name: winner.name || '',
                    win_flag2: winner.flag2 || '',
                    win_flag3: winner.flag3 || '',
                    win_score: data.win_score
                };
            },
        },
        {
            id: 'instant-replay',
            selector: '#widget-ivr',
            events: { show: 'show_ivr', hide: 'hide_ivr' },
            bindsData: false,
        },
    ],
};