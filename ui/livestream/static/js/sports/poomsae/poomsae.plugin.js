/**
 * Poomsae overlay plugin.
 *
 * Fed by the Poomsae Livestream Extension:
 *   listener_update   { data: <live payload> }          → bindData + onUpdate
 *   clock_update      { data: { clk } }                  (negative = overtime)
 *   scoreboard_sync   { data: { visible, header_only, result, compact,
 *                               clock_hidden, show_poomsae_name, show_results } }
 *                     → classes + show/hide; re-sent every few seconds so a
 *                     reloaded browser source catches up on its own
 *   deduction_flash   { data: { corner, amount } }      → "-0.3" next to the score,
 *                     only when a majority of judges made that call together
 *   athlete_intro     { data: { athletes, phase, … } }  + show_/hide_athlete_intro
 *                     → name card at the start of Poomsae 1 (once per match)
 *   compact_scoreboard / uncompact_scoreboard           → short names
 *
 * Stages: Preparation → scoreboard header only (number + category);
 * Poomsae → full scoreboard; Results → scoreboard hidden, Results widget
 * (kyorugi style: category header, accuracy + presentation per judge).
 *
 * The scoreboard number is the judges' average accuracy while the athlete
 * performs (starts at 4.00).
 */

const DROP_FLASH_MS = 2500;
const TWEEN_MS = 450;
const EPS = 0.0005;
const FLAG_BASE = 'https://flagcdn.com/w80/';
const JUDGE_COL = '52px';
const TOTAL_COL = '96px';
const FINAL_COL = '84px';

const shown = new Map(); // corner → round key last rendered

const fmt = (n) => Number(n).toFixed(2);
const esc = (v) => String(v ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const hasFlag = (c) => c?.flag2 && c.flag2 !== 'un';
const barClass = (color, isSolo) => (isSolo ? 'bg-green' : `bg-${color}`);

// ------------------------------------------------------------------ scoreboard

function tweenNumber(el, to) {
    const from = Number(el.dataset.value ?? to);
    el.dataset.value = to;
    cancelAnimationFrame(el._raf);
    if (Math.abs(from - to) < EPS) {
        el.textContent = fmt(to);
        return;
    }
    const start = performance.now();
    const step = (now) => {
        const t = Math.min(1, (now - start) / TWEEN_MS);
        const eased = 1 - Math.pow(1 - t, 3);
        el.textContent = fmt(from + (to - from) * eased);
        if (t < 1) el._raf = requestAnimationFrame(step);
    };
    el._raf = requestAnimationFrame(step);
}

function pulse(el) {
    if (!el) return;
    el.classList.remove('is-dropping');
    void el.offsetWidth;
    el.classList.add('is-dropping');
}

/** "-0.3" next to the score. A new call replaces the one still showing. */
function flashDeduction(root, corner, amount) {
    const scores = root.querySelector(`.score-elements[data-corner="${corner}"]`);
    if (!scores) return;
    let box = scores.querySelector('.pms-drop');
    if (!box) {
        box = document.createElement('div');
        box.className = 'event-box pms-drop';
        scores.appendChild(box);
    } else {
        clearTimeout(box._timer);
        box.style.animation = 'none';
        void box.offsetWidth;
        box.style.animation = '';
    }
    box.textContent = `-${Number(amount).toFixed(1)}`;
    box._timer = setTimeout(() => box.remove(), DROP_FLASH_MS);
    pulse(scores.querySelector('.pms-total'));
}

function renderCorner(root, color, c, ctx) {
    const bar = root.querySelector(`.color-bar[data-corner="${color}"]`);
    const info = root.querySelector(`.competitor-info[data-corner="${color}"]`);
    const scores = root.querySelector(`.score-elements[data-corner="${color}"]`);
    if (!c || !scores) return;

    bar?.classList.remove('bg-blue', 'bg-red', 'bg-green');
    bar?.classList.add(barClass(color, ctx.solo));
    info?.classList.toggle('no-country', !hasFlag(c));

    const total = scores.querySelector('.pms-total');
    const totalMain = total.querySelector('.main');
    const value = Number(c.display_score ?? c.accuracy ?? 4);

    if (shown.get(color) === ctx.key) {
        tweenNumber(totalMain, value);
    } else {
        cancelAnimationFrame(totalMain._raf);
        totalMain.dataset.value = value;
        totalMain.textContent = fmt(value);
        scores.querySelectorAll('.pms-drop').forEach((b) => b.remove());
        shown.set(color, ctx.key);
    }

    // Restart / penalty total for this poomsae (yellow box).
    const penalty = scores.querySelector('.pms-penalty');
    const deduction = Number(c.deduction || 0);
    penalty?.classList.toggle('is-hidden', !(deduction < 0));
    if (penalty) penalty.querySelector('.main').textContent = deduction.toFixed(1);
}

// ------------------------------------------------------------------ results

function judgeBoxes(judges, field) {
    return judges.map((j) => {
        const cls = ['point-box', 'pms-judge'];
        if (j[`trim_${field}`]) cls.push('is-trimmed');
        if (!j.submitted) cls.push('is-pending');
        return `<div class="${cls.join(' ')}"><i class="main">${fmt(j[field])}</i></div>`;
    }).join('');
}

/** Column labels (J1 … / P1 P2 / TOTAL) sitting in the dark header bar,
 *  right above the boxes they belong to. Returns [html, width in px]. */
function headerColumns(labels, colWidth, lastWidth) {
    const cols = `repeat(${labels.length - 1}, ${colWidth}) ${lastWidth}`;
    const width = (labels.length - 1) * (parseInt(colWidth, 10) + 2) + parseInt(lastWidth, 10);
    const html = `<div class="pms-head-cols" style="grid-template-columns:${cols}">${labels.map((l) => `<div>${esc(l)}</div>`).join('')}</div>`;
    return [html, width];
}

/** One athlete: a big name header (country, flag, name — like the intro /
 *  previous-results card, no colour bar), then an accuracy and a
 *  presentation row with one box per judge and a tall total. */
function resultBlock(c, color, data, judgeCount, withColumns) {
    const judges = (c.judge_scores || []).slice(0, judgeCount);
    while (judges.length < judgeCount) judges.push({ judge: judges.length + 1, accuracy: 0, presentation: 0, submitted: false });
    const deduction = Number(c.deduction || 0);
    const leads = data.is_solo || data.leader === color;
    const bar = barClass(color, data.is_solo);
    const [cols, colsWidth] = withColumns
        ? headerColumns([...judges.map((j) => `J${j.judge}`), 'Total'], JUDGE_COL, TOTAL_COL)
        : ['', 0];
    return `
        <div class="header-grid pms-result-head">
            <div class="header-content" style="padding-right:${colsWidth ? colsWidth + 20 : 15}px">
                ${hasFlag(c) ? `<div class="header-country">${esc(c.flag3)}</div>
                <img class="flag-lg" src="${FLAG_BASE}${esc(c.flag2)}.png" alt="">` : ''}
                <div class="header-name">${esc(c.name)}</div>
                ${cols}
            </div>
        </div>
        <div class="pms-result-block" style="grid-template-columns:10px 1fr repeat(${judgeCount}, ${JUDGE_COL}) ${TOTAL_COL}">
            <div class="color-bar ${bar} pms-span2"></div>
            <div class="row-content pms-res-row">
                <div class="pms-row-label">Accuracy</div>
                <div class="pms-row-value">${fmt(c.accuracy)}</div>
            </div>
            ${judgeBoxes(judges, 'accuracy')}
            <div class="point-box pms-total-box pms-span2${leads ? ' winner' : ''}"><i class="main">${fmt(c.score)}</i></div>
            <div class="row-content pms-res-row">
                <div class="pms-row-label">Presentation${deduction < 0 ? ` <span class="pms-ded">${deduction.toFixed(1)}</span>` : ''}</div>
                <div class="pms-row-value">${fmt(c.presentation)}</div>
            </div>
            ${judgeBoxes(judges, 'presentation')}
        </div>`;
}

/** Rebuilt only when the numbers change, so the reveal animation never restarts mid-way. */
function renderResults(el, data) {
    if (!data?.result) return;
    const body = el.querySelector('.pms-results-body');
    if (!body) return;
    const corners = (data.is_solo ? ['blue'] : ['blue', 'red']).filter((k) => data[k]);
    const signature = JSON.stringify([data.match_id, data.round_key, data.leader, corners.map((k) => data[k])]);
    if (body.dataset.signature === signature) return;
    body.dataset.signature = signature;
    const judgeCount = Math.max(1, ...corners.map((k) => (data[k].judge_scores || []).length));
    body.innerHTML = corners.map((k, i) => resultBlock(data[k], k, data, judgeCount, i === 0)).join('');
}

// ------------------------------------------------------------------ final score

/** Kyorugi "Winner" look, kept minimal: the header bar says FINAL SCORE and
 *  carries the P1 / P2 / TOTAL labels; one row per athlete below. */
function renderFinal(el, data) {
    const f = data?.final;
    const body = el.querySelector('.pms-final-body');
    const head = el.querySelector('.pms-final-head');
    if (!f || !body) return;
    const signature = JSON.stringify([data.match_id, f]);
    if (body.dataset.signature === signature) return;
    body.dataset.signature = signature;

    if (head) {
        const [cols, width] = headerColumns([...f.columns.map((c) => c.label), 'Total'], FINAL_COL, TOTAL_COL);
        head.querySelector('.pms-head-cols')?.remove();
        head.insertAdjacentHTML('beforeend', cols);
        head.style.paddingRight = `${width + 20}px`;
    }

    const cols = `10px 1fr repeat(${f.columns.length}, ${FINAL_COL}) ${TOTAL_COL}`;
    body.innerHTML = f.athletes.map((a) => `
        <div class="row-wrapper pms-final-row" style="grid-template-columns:${cols}">
            <div class="color-bar ${barClass(a.corner, data.is_solo)}"></div>
            <div class="row-content">
                <div class="competitor-info${hasFlag(a) ? '' : ' no-country'}">
                    <div class="res-country">${esc(a.flag3)}</div>
                    ${hasFlag(a) ? `<img class="flag-sm" src="https://flagcdn.com/w40/${esc(a.flag2)}.png" alt="">` : '<i></i>'}
                    <div class="res-name">${esc(a.name)}</div>
                </div>
            </div>
            ${a.poomsae.map((x) => `<div class="point-box pms-final-box"><i class="main">${fmt(x)}</i></div>`).join('')}
            <div class="point-box pms-final-total${f.leader === a.corner ? ' winner' : ''}"><i class="main">${fmt(a.total)}</i></div>
        </div>`).join('');
}

// ------------------------------------------------------------------ category results

/** Ranked list under a big category header: [rank] [country flag name] [score].
 *  Up to 5 rows it sits at the bottom like every other widget; with more it
 *  is centred on screen so a long list never runs off the top. */
const CATEGORY_BOTTOM_MAX = 5;

function renderCategory(el, data) {
    const rows = data?.category_results?.rows;
    const body = el.querySelector('.pms-cat-body');
    if (!rows || !body) return;
    const signature = JSON.stringify([data.match_id, rows]);
    if (body.dataset.signature === signature) return;
    body.dataset.signature = signature;

    el.classList.toggle('is-centered', rows.length > CATEGORY_BOTTOM_MAX);
    body.innerHTML = rows.map((r) => `
        <div class="row-wrapper pms-cat-row${r.current ? ' is-current' : ''}${r.qualified ? ' is-qualified' : ''}">
            <div class="point-box pms-cat-rank"><i class="main">${esc(r.rank ?? '')}</i></div>
            <div class="row-content">
                <div class="competitor-info${hasFlag(r) ? '' : ' no-country'}">
                    <div class="res-country">${esc(r.flag3)}</div>
                    ${hasFlag(r) ? `<img class="flag-sm" src="https://flagcdn.com/w40/${esc(r.flag2)}.png" alt="">` : '<i></i>'}
                    <div class="res-name">${esc(r.name)}</div>
                </div>
            </div>
            <div class="point-box pms-cat-score"><i class="main">${r.score == null ? '' : fmt(r.score)}</i></div>
        </div>`).join('');
}

// ------------------------------------------------------------------ athlete intro

function renderIntro(el, d) {
    const body = el.querySelector('.pms-intro-body');
    const label = el.querySelector('.header-label');
    if (!body || !d) return;
    if (label) {
        label.textContent = d.phase || '';
        label.classList.toggle('is-hidden', !d.phase);
    }
    body.innerHTML = (d.athletes || []).map((a) => `
        <div class="header-grid with-color">
            <div class="color-bar ${barClass(a.corner, d.is_solo)}"></div>
            <div class="header-content">
                ${hasFlag(a) ? `<div class="header-country">${esc(a.flag3)}</div>
                <img class="flag-lg" src="${FLAG_BASE}${esc(a.flag2)}.png" alt="">` : ''}
                <div class="header-name">${esc(a.name)}</div>
                <div class="header-seed">${esc(a.club)}</div>
                <img class="header-logo" src="assets/icons/watermark.png" alt="Logo">
            </div>
        </div>`).join('');
}

// ------------------------------------------------------------------ widgets

export const poomsaePlugin = {
    widgets: [
        {
            id: 'scoreboard',
            selector: '#widget-scoreboard-system',
            events: { show: 'show_scoreboard', hide: 'hide_scoreboard' },
            // Same events kyorugi uses: country code hidden, short names.
            stateEvents: {
                'is-compact': { on: 'compact_scoreboard', off: 'uncompact_scoreboard' },
                'clock-hidden': { on: 'hide_clock', off: 'show_clock' },
            },
            onUpdate: (el, data) => {
                if (!data) return;
                const ctx = { key: `${data.match_id}|${data.round_key}`, solo: !!data.is_solo };
                el.classList.toggle('is-solo', ctx.solo);
                // Para / freestyle: no accuracy score, no deductions on the scoreboard.
                el.classList.toggle('no-live-score', data.live_score === false);
                renderCorner(el, 'blue', data.blue, ctx);
                if (!ctx.solo) renderCorner(el, 'red', data.red, ctx);
            },
            customEvents: {
                clock_update: (widget, payload) => {
                    const clk = payload?.data?.clk;
                    if (clk === undefined) return;
                    widget.el.querySelectorAll('[data-bind="clk"]').forEach((el) => {
                        el.textContent = clk;
                        el.classList.toggle('is-overtime', String(clk).startsWith('-'));
                    });
                },
                deduction_flash: (widget, payload) => {
                    const d = payload?.data;
                    if (d?.corner && d?.amount) flashDeduction(widget.el, d.corner, d.amount);
                },
                scoreboard_sync: (widget, payload) => {
                    const d = payload?.data || {};
                    widget.el.classList.toggle('is-result', !!d.result);
                    if (typeof d.header_only === 'boolean') widget.el.classList.toggle('is-header-only', d.header_only);
                    if (typeof d.compact === 'boolean') widget.el.classList.toggle('is-compact', d.compact);
                    if (typeof d.clock_hidden === 'boolean') widget.el.classList.toggle('clock-hidden', d.clock_hidden);
                    if (typeof d.presentation === 'boolean') widget.el.classList.toggle('is-presentation', d.presentation);
                    widget.controller.toggle(!!d.visible);
                    widget.registry.get('poomsae-name')?.controller.toggle(!!d.show_poomsae_name);
                    widget.registry.get('round-results')?.controller.toggle(!!d.show_results);
                    widget.registry.get('final-score')?.controller.toggle(!!d.show_final);
                    widget.registry.get('category-results')?.controller.toggle(!!d.show_category);
                },
            },
        },
        {
            // Drawn poomsae, bottom-left corner (shown/hidden by scoreboard_sync).
            id: 'poomsae-name',
            selector: '#widget-poomsae-name',
            events: { show: 'show_poomsae_name', hide: 'hide_poomsae_name' },
        },
        {
            // Results 1 / 2 (shown/hidden by scoreboard_sync).
            id: 'round-results',
            selector: '#widget-round-results',
            events: { show: 'show_round_results', hide: 'hide_round_results' },
            prepareData: (data) => ({
                ...data,
                // The drawn poomsae's name; "Poomsae 1" / "Poomsae 2" when none was drawn.
                results_title: data.poomsae_name || `Poomsae ${data.poomsae_index ?? 1}`,
            }),
            onUpdate: (el, data) => renderResults(el, data),
        },
        {
            // Final Results: P1 / P2 totals + overall (shown/hidden by scoreboard_sync).
            id: 'final-score',
            selector: '#widget-final-score',
            events: { show: 'show_final_score', hide: 'hide_final_score' },
            prepareData: (data) => ({
                ...data,
                header_line: [data.id_formatted, data.category].filter(Boolean).join(' '),
            }),
            onUpdate: (el, data) => renderFinal(el, data),
        },
        {
            // Category Results: ranking (shown/hidden by scoreboard_sync).
            id: 'category-results',
            selector: '#widget-category-results',
            events: { show: 'show_category_results', hide: 'hide_category_results' },
            onUpdate: (el, data) => renderCategory(el, data),
        },
        {
            // Name card at the start of Poomsae 1 (once per match).
            id: 'athlete-intro',
            selector: '#widget-athlete-intro',
            events: { show: 'show_athlete_intro', hide: 'hide_athlete_intro' },
            bindsData: false,
            customEvents: {
                athlete_intro: (widget, payload) => renderIntro(widget.el, payload?.data),
            },
        },
    ],
};