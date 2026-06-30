const sbWidget = document.querySelector('#widget-scoreboard-system');
let sbBusy = false;

function showScoreboard() {
    if (sbBusy || !sbWidget.classList.contains('hidden')) return;

    sbBusy = true;
    sbWidget.classList.remove('hidden', 'sb-exiting');
    sbWidget.classList.add('sb-animating', 'sb-entering');

    void sbWidget.offsetWidth; // Force DOM reflow to restart animations

    // Sequence completes around 1s. Leave buffer.
    setTimeout(() => {
        sbWidget.classList.remove('sb-entering', 'sb-animating');
        sbBusy = false;
    }, 1200);
}

function hideScoreboard() {
    if (sbBusy || sbWidget.classList.contains('hidden')) return;

    sbBusy = true;
    sbWidget.classList.remove('sb-entering');
    sbWidget.classList.add('sb-animating', 'sb-exiting');

    // Matches the 0.4s retract animation
    setTimeout(() => {
        sbWidget.classList.remove('sb-exiting', 'sb-animating');
        sbWidget.classList.add('hidden');
        sbBusy = false;
    }, 400);
}

const fbWidget = document.querySelector('#widget-fighter-bars');
let fbBusy = false;

function showFighterBars() {
    if (fbBusy || !fbWidget.classList.contains('hidden')) return;

    fbBusy = true;
    fbWidget.classList.remove('hidden', 'fb-exiting');
    fbWidget.classList.add('fb-animating', 'fb-entering');

    void fbWidget.offsetWidth; // Force DOM reflow to restart animations

    // Total animation time is 0.35s delay + 0.5s duration = 0.85s. Setting timeout to 1000ms for safety.
    setTimeout(() => {
        fbWidget.classList.remove('fb-entering', 'fb-animating');
        fbBusy = false;
    }, 1000);
}

function hideFighterBars() {
    if (fbBusy || fbWidget.classList.contains('hidden')) return;

    fbBusy = true;
    fbWidget.classList.remove('fb-entering');
    fbWidget.classList.add('fb-animating', 'fb-exiting');

    setTimeout(() => {
        fbWidget.classList.remove('fb-exiting', 'fb-animating');
        fbWidget.classList.add('hidden');
        fbBusy = false;
    }, 380);
}

const rrWidget = document.querySelector('#widget-round-results');
let rrBusy = false;

function showRoundResults() {
    if (rrBusy || !rrWidget.classList.contains('hidden')) return;

    rrBusy = true;
    rrWidget.classList.remove('hidden', 'rr-exiting');
    rrWidget.classList.add('rr-animating', 'rr-entering');

    void rrWidget.offsetWidth; // Force DOM reflow to restart animations

    // Clean up animation classes after the sequence finishes (~1.1s total)
    setTimeout(() => {
        rrWidget.classList.remove('rr-entering', 'rr-animating');
        rrBusy = false;
    }, 1200);
}

function hideRoundResults() {
    if (rrBusy || rrWidget.classList.contains('hidden')) return;

    rrBusy = true;
    rrWidget.classList.remove('rr-entering');
    rrWidget.classList.add('rr-animating', 'rr-exiting');

    setTimeout(() => {
        rrWidget.classList.remove('rr-exiting', 'rr-animating');
        rrWidget.classList.add('hidden');
        rrBusy = false;
    }, 380);
}

const winWidget = document.querySelector('#widget-winner');
let winBusy = false;

function showWinner() {
    if (winBusy || !winWidget.classList.contains('hidden')) return;

    winBusy = true;
    winWidget.classList.remove('hidden', 'win-exiting');
    winWidget.classList.add('win-animating', 'win-entering');

    void winWidget.offsetWidth; // Force DOM reflow to restart animations

    // Clean up animation classes after the sequence finishes (~1.1s total)
    setTimeout(() => {
        winWidget.classList.remove('win-entering', 'win-animating');
        winBusy = false;
    }, 1200);
}

function hideWinner() {
    if (winBusy || winWidget.classList.contains('hidden')) return;

    winBusy = true;
    winWidget.classList.remove('win-entering');
    winWidget.classList.add('win-animating', 'win-exiting');

    setTimeout(() => {
        winWidget.classList.remove('win-exiting', 'win-animating');
        winWidget.classList.add('hidden');
        winBusy = false;
    }, 380);
}

const vr = document.querySelector('#widget-ivr');
let vrBusy = false;

function showVR() {
    if (vrBusy || !vr.classList.contains('hidden')) return;
    vrBusy = true;
    vr.classList.remove('hidden', 'vr-exiting');
    vr.classList.add('vr-animating', 'vr-entering');
    void vr.offsetWidth; // force reflow
    setTimeout(() => {
        vr.classList.remove('vr-entering', 'vr-animating');
        vrBusy = false;
    }, 1200);
}

function hideVR() {
    if (vrBusy || vr.classList.contains('hidden')) return;
    vrBusy = true;
    vr.classList.remove('vr-entering');
    vr.classList.add('vr-animating', 'vr-exiting');
    setTimeout(() => {
        vr.classList.remove('vr-exiting', 'vr-animating');
        vr.classList.add('hidden');
        vrBusy = false;
    }, 380);
}

function showYT() {
    toggleWidget("widget-yt", true)
}

function hideYT() {
    toggleWidget("widget-yt", false)
}

// --- Core Widget Management ---
function toggleWidget(widgetId, show) {
    const widget = document.getElementById(widgetId);
    if (widget) {
        show ? widget.classList.remove("hidden") : widget.classList.add("hidden");
    }
}

// --- DOM Manipulation Helpers ---
// Safely updates text content if the element exists
function updateText(elementId, text) {
    const el = document.getElementById(elementId);
    if (el && text !== undefined && text !== null) {
        el.innerText = text;
    }
}

// Safely updates image sources (for flags)
function updateImage(elementId, flagCode) {
    const el = document.getElementById(elementId);
    // Assuming flagCode is an alpha2 code like "kr" or "sk"
    if (el && flagCode && flagCode !== "un") {
        el.src = `https://flagcdn.com/w80/${flagCode}.png`;
    }
}

function updateColor(elementId, color) {
    const el = document.getElementById(elementId);
    // Assuming flagCode is an alpha2 code like "kr" or "sk"
    if (color == "blue") {
        el.classList.remove("bg-red")
        el.classList.add("bg-blue")
    } else if (color == "red") {
        el.classList.remove("bg-blue")
        el.classList.add("bg-red")
    }
}

// --- Specific Widget Updaters ---
// Break your updates into logical blocks so it's easy to read and maintain
function updateNextRoundWidget(data) {
    updateText("nr-match-id", data.id);
    updateText("nr-blue-name", data.blue_name);
    updateText("nr-red-name", data.red_name);
}

function updateWinnerWidget(data) {
    // Example: Combining ID and Category for the header
    updateText("win-header", `${data.id} ${data.category}`);
    updateText("win-title", `${data.title}`);

    // Note: You'll need logic to determine IF blue or red won.
    // For now, let's assume we are just filling in placeholders.
    if (data.win == "blue") {
        updateText("win-name", data.blue_name);
        updateImage("win-flag", data.blue_flag2);
        updateText("win-country", data.blue_flag3.toUpperCase());
    } else if (data.win == "red") {
        updateText("win-name", data.red_name);
        updateImage("win-flag", data.red_flag2);
        updateText("win-country", data.red_flag3.toUpperCase());
    }

    updateColor("win-color", data.win)
}

function updateRoundResultsWidget(data) {
    updateText("rr-header", "RESULTS ROUND " + data.round);
    updateText("rr-category", data.category);
    updateText("rr-title", data.title);

    // Blue Athlete
    updateText("rr-blue-name", data.blue_name);
    updateImage("rr-blue-flag", data.blue_flag2);
    updateText("rr-blue-country", data.blue_flag3.toUpperCase());

    updateText("rr-blue-points", data.blue_points[data.round - 1].points);
    updateText("rr-blue-hits", data.blue_points[data.round - 1].hits);
    updateText("rr-blue-trunk", data.blue_points[data.round - 1].trunk);
    updateText("rr-blue-rotation-trunk", data.blue_points[data.round - 1].rotation_trunk);
    updateText("rr-blue-head", data.blue_points[data.round - 1].head);
    updateText("rr-blue-rotation-head", data.blue_points[data.round - 1].rotation_head);
    updateText("rr-blue-punch", data.blue_points[data.round - 1].punch);
    updateText("rr-blue-penalties", data.blue_points[data.round - 1].penalties);

    // Red Athlete
    updateText("rr-red-name", data.red_name);
    updateImage("rr-red-flag", data.red_flag2);
    updateText("rr-red-country", data.red_flag3.toUpperCase());

    updateText("rr-red-points", data.red_points[data.round - 1].points);
    updateText("rr-red-hits", data.red_points[data.round - 1].hits);
    updateText("rr-red-trunk", data.red_points[data.round - 1].trunk);
    updateText("rr-red-rotation-trunk", data.red_points[data.round - 1].rotation_trunk);
    updateText("rr-red-head", data.red_points[data.round - 1].head);
    updateText("rr-red-rotation-head", data.red_points[data.round - 1].rotation_head);
    updateText("rr-red-punch", data.red_points[data.round - 1].punch);
    updateText("rr-red-penalties", data.red_points[data.round - 1].penalties);

    // You can expand this to map through data.blue_points array to update point boxes
}

function updateFighterBarsWidget(data) {
    // Blue Athlete
    updateText("fb-blue-name", data.blue_name);
    updateImage("fb-blue-flag", data.blue_flag2);
    if (data.blue_flag3) updateText("fb-blue-country", data.blue_flag3.toUpperCase());
    updateText("fb-blue-seed", data.blue_seed ? `(${data.blue_seed})` : "");

    // Red Athlete
    updateText("fb-red-name", data.red_name);
    updateImage("fb-red-flag", data.red_flag2);
    if (data.red_flag3) updateText("fb-red-country", data.red_flag3.toUpperCase());
    updateText("fb-red-seed", data.red_seed ? `(${data.red_seed})` : "");
}

// --- Main Data Dispatcher ---
function handleDataUpdate(payload) {
    // In Python, you emit: {"event": "update", "data": self.data}
    // So we need to access payload.data
    const data = payload.data;
    if (!data) return;

    // Call individual updaters
    updateNextRoundWidget(data);
    updateWinnerWidget(data);
    updateRoundResultsWidget(data);
    updateFighterBarsWidget(data);

    console.log("UI Updated with match ID:", data.id);
}

function resetWidgets(payload) {
    console.log(payload)
    const data = payload.data;
    if (!data) {
        toggleWidget("widget-scoreboard-system", false)
        hideVR()
        toggleWidget("widget-round-results", false)
        toggleWidget("widget-winner", false)
        toggleWidget("widget-fighter-bars")
    } else {
        data.forEach(element => {
            toggleWidget(element, false)
        });
    }

}

let tickerCount = 0;
let maxTickerRounds = 3;

function startTicker(message, rounds = 3) {
    const ticker = document.getElementById('widget-ticker');
    const wrapper = document.getElementById('ticker-wrapper');
    const text = document.getElementById('ticker-msg');

    // Set content and reset counters
    text.innerText = message;
    tickerCount = 0;
    maxTickerRounds = rounds;

    // Show the bar (Rise animation)
    ticker.classList.add('active');

    // Start the scroll animation after a short delay
    setTimeout(() => {
        wrapper.classList.add('scrolling');
    }, 500);

    // Listener for when one full scroll finishes
    wrapper.onanimationiteration = () => {
        tickerCount++;
        if (tickerCount >= maxTickerRounds) {
            stopTicker();
        }
    };

    // Fallback for single-run mode
    wrapper.onanimationend = () => {
        tickerCount++;
        if (tickerCount >= maxTickerRounds) {
            stopTicker();
        } else {
            // Restart animation if more rounds needed
            wrapper.classList.remove('scrolling');
            void wrapper.offsetWidth; // Trigger reflow
            wrapper.classList.add('scrolling');
        }
    };
}

function stopTicker() {
    const ticker = document.getElementById('widget-ticker');
    const wrapper = document.getElementById('ticker-wrapper');

    // Sink the bar
    ticker.classList.remove('active');

    // Wait for sink animation to finish before removing scroll
    setTimeout(() => {
        wrapper.classList.remove('scrolling');
    }, 500);
}

// --- Socket Connection & Listeners ---
const socket = io("http://localhost:8000/truevar");

socket.on("connect", () => {
    console.log("WebSocket Connected");
});

socket.onAny((eventName, ...args) => {
    console.log(`[SOCKET RX] Event: '${eventName}'`, args);
});

socket.on("disconnect", (reason) => {
    console.warn(`[SOCKET DISCONNECT] Reason:`, reason);
});

// Display Listeners
socket.on("show_next_round", () => showScoreboard());
socket.on("hide_next_round", () => hideScoreboard());

socket.on("show_ivr", () => showVR());
socket.on("hide_ivr", () => hideVR());

socket.on("show_fighter_bars", () => showFighterBars());
socket.on("hide_fighter_bars", () => hideFighterBars());

socket.on("show_round_results", () => showRoundResults());
socket.on("hide_round_results", () => hideRoundResults());

socket.on("show_win", () => showWinner());
socket.on("hide_win", () => hideWinner());

socket.on("show_yt", () => showYT());
socket.on("hide_yt", () => hideYT());

socket.on("show_ticker", (data) => { startTicker(data.message, 3); });
socket.on("hide_ticker", () => { stopTicker(); });

socket.on("reset_widgets", (payload) => {
    if (payload.event === "reset") {
        resetWidgets(payload);
    }
});

// Data Listener
socket.on("listener_update", (payload) => {
    if (payload.event === "update") {
        handleDataUpdate(payload);
    }
});