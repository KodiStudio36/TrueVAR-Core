function fmtId(id) {
    if (id < 100 && id > 9) {
        return "0" + id;
    } else if (id < 10) {
        return "00" + id;
    }
}

function fmtRound(round) {
    switch (round) {
        case 1:
            return "1st";
        case 2:
            return "2nd";
        case 3:
            return "3rd";
        default:
            return "1st";
    }
}

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

const nxWidget = document.querySelector('#widget-next-round');
let nxBusy = false;

function showNextRound() {
    if (nxBusy || !nxWidget.classList.contains('hidden')) return;

    nxBusy = true;
    nxWidget.classList.remove('hidden', 'sb-exiting');
    nxWidget.classList.add('sb-animating', 'sb-entering');

    void nxWidget.offsetWidth; // Force DOM reflow to restart animations

    // Sequence completes around 1s. Leave buffer.
    setTimeout(() => {
        nxWidget.classList.remove('sb-entering', 'sb-animating');
        nxBusy = false;
    }, 1200);
}

function hideNextRound() {
    if (nxBusy || nxWidget.classList.contains('hidden')) return;

    nxBusy = true;
    nxWidget.classList.remove('sb-entering');
    nxWidget.classList.add('sb-animating', 'sb-exiting');

    // Matches the 0.4s retract animation
    setTimeout(() => {
        nxWidget.classList.remove('sb-exiting', 'sb-animating');
        nxWidget.classList.add('hidden');
        nxBusy = false;
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
function updateScoreboard(data) {
    // Blue Athlete
    updateText("sb-blue-name", data.blue_name);
    updateImage("sb-blue-flag", data.blue_flag2);
    updateText("sb-blue-country", data.blue_flag3.toUpperCase());

    // Red Athlete
    updateText("sb-red-name", data.red_name);
    updateImage("sb-red-flag", data.red_flag2);
    updateText("sb-red-country", data.red_flag3.toUpperCase());

    updateText("sb-match-id", fmtId(data.id));
    updateText("sb-category", data.category);
    updateText("sb-round", fmtRound(data.round));
}

function updateClock(data) {
    updateText("sb-clock", data.clk);
}

function updateNextRoundWidget(data) {
    updateText("nr-match-id", fmtId(data.id));
    updateText("nr-blue-name", data.blue_name);
    updateText("nr-red-name", data.red_name);
}

function updateWinnerWidget(data) {
    // Example: Combining ID and Category for the header
    updateText("win-header", `${fmtId(data.id)} ${data.category}`);
    updateText("win-id", fmtId(data.id));

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
    updateScoreboard(data);
    updateClock(data);
    updateNextRoundWidget(data);
    updateWinnerWidget(data);
    updateFighterBarsWidget(data);

    console.log("UI Updated with match ID:", fmtId(data.id));
}

function resetWidgets(payload) {
    console.log(payload)
    const data = payload.data;
    if (!data) {
        hideScoreboard()
        hideNextRound()
        hideFighterBars()
        hideWinner()
    } else {
        data.forEach(element => {
            toggleWidget(element, false)
        });
    }

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
socket.on("show_scoreboard", () => showScoreboard());
socket.on("hide_scoreboard", () => hideScoreboard());

socket.on("show_next_round", () => showNextRound());
socket.on("hide_next_round", () => hideNextRound());

socket.on("show_fighter_bars", () => showFighterBars());
socket.on("hide_fighter_bars", () => hideFighterBars());

socket.on("show_win", () => showWinner());
socket.on("hide_win", () => hideWinner());

socket.on("show_yt", () => showYT());
socket.on("hide_yt", () => hideYT());

socket.on("reset_widgets", (payload) => {
    if (payload.event === "reset") {
        resetWidgets(payload);
    }
});

// Data Listener
socket.on("stable_update", (payload) => {
    if (payload.event === "update") {
        handleDataUpdate(payload);
    }
});

socket.on("clock_update", (payload) => {
    if (payload.event === "update") {
        console.log("here");
        updateClock(payload.data);
    }
});