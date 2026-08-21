/**
 * Declarative data binding.
 *
 * Instead of every sport writing its own updateScoreboard(),
 * updateWinnerWidget(), updateFighterBarsWidget()... a widget's
 * HTML just marks which field feeds which element:
 *
 *   <div data-bind="blue_name"></div>
 *   <img  data-bind-img="blue_flag2">
 *   <div  data-bind-color="win">
 *   <div  data-bind-format="id:matchId"></div>
 *
 * bindData() walks those attributes once. New fields never need
 * new JS — only a new attribute in the sport's HTML/plugin.
 */

const FLAG_BASE = 'https://flagcdn.com/w80/';

export const formatters = {
    matchId: (id) => String(id).padStart(3, '0'),
    round: (r) => ({ 1: '1st', 2: '2nd', 3: '3rd' }[r] || `${r}th`),
    upper: (s) => String(s).toUpperCase(),
    category: (str) => {
        if (!str) return '';
        return str
            .replace(/_?(under|minus)_/gi, ' -')
            .replace(/_?(plus|over)_/gi, ' +')
            .replace(/_/g, ' ')
            .trim()
            .replace(/\b[a-z]/g, (char) => char.toUpperCase());
    },
};

function resolve(data, path) {
    return path.split('.').reduce((acc, key) => (acc == null ? acc : acc[key]), data);
}

export function bindData(root, data) {
    if (!data) return;

    root.querySelectorAll('[data-bind]').forEach((el) => {
        const val = resolve(data, el.dataset.bind);
        if (val !== undefined && val !== null) el.textContent = val;
    });

    root.querySelectorAll('[data-bind-img]').forEach((el) => {
        const code = resolve(data, el.dataset.bindImg);
        if (code && code !== 'un') el.src = `${FLAG_BASE}${code}.png`;
    });

    root.querySelectorAll('[data-bind-color]').forEach((el) => {
        const color = resolve(data, el.dataset.bindColor);
        if (color === 'blue' || color === 'red') {
            el.classList.remove('bg-blue', 'bg-red');
            el.classList.add(`bg-${color}`);
        }
    });

    root.querySelectorAll('[data-bind-format]').forEach((el) => {
        const [path, formatterName] = el.dataset.bindFormat.split(':');
        const val = resolve(data, path);
        const fn = formatters[formatterName];
        if (fn && val !== undefined && val !== null) el.textContent = fn(val);
    });
}