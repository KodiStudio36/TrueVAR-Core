ALPHA3_TO_ALPHA2 = {
    "KOR": "kr", "USA": "us", "FRA": "fr", "GER": "de", "ESP": "es",
    "GBR": "gb", "ITA": "it", "CHN": "cn", "MEX": "mx", "BRA": "br"
}

INIT_STATE = "init"
READY_STATE = "ready"
FIGHT_STATE = "fight"
BREAK_STATE = "brk"

class TkStrikeStateEngine:
    """Encapsulates the exact state machine and parsing arrays from the old worker."""
    def __init__(self, on_stable_update, on_clock_update, on_event_trigger):
        self.on_stable_update = on_stable_update
        self.on_clock_update = on_clock_update
        self.on_event_trigger = on_event_trigger
        
        self.complete_data_counter = 0
        self.clk_default = "02:00"
        self.data = {}
        self.reset_data()

        # Command Flag Lookup Table
        self.flags = {
            "mch": self.on_match,
            "at1": self.on_athletes,
            "rnd": self.on_round,
            "rdy": self.on_ready,
            "hwt": self.on_test,
            "clk": self.on_clock,
            "sc1": self.on_scoreboard,
            "wg1": self.on_penalty,
            "hl1": self.on_blue_hit,
            "hl2": self.on_red_hit,
            "pt1": self.on_blue_scores,
            "pt2": self.on_red_scores,
            "brk": self.on_break,
            "win": self.on_win,
        }

    def parse_string(self, msg: str):
        parts = msg.strip().split(";")
        if not parts or not parts[0]:
            return
        command = parts[0].lower()
        handler = self.flags.get(command)
        if handler:
            handler(parts)

    # --- Parser Action Methods ---
    def on_match(self, parts):
        if self.complete_data_counter == 0: 
            self.reset_data()
        self.data.update({
            "id": parts[1], "title": parts[2], "category": parts[3], "hit_level": parts[14],
        })
        self.evaluate_match_init()

    def on_athletes(self, parts):
        if self.complete_data_counter == 0: 
            self.reset_data()
        self.data.update({
            "blue_name": parts[1], "blue_flag2": ALPHA3_TO_ALPHA2.get(parts[3], "un").lower(), "blue_flag3": parts[3],
            "red_name": parts[5], "red_flag2": ALPHA3_TO_ALPHA2.get(parts[7], "un").lower(), "red_flag3": parts[7],
        })
        self.evaluate_match_init()

    def on_round(self, parts):
        self.data["round"] = int(parts[1])
        if self.data["state"] in [FIGHT_STATE, BREAK_STATE]:
            self.on_stable_update(self.data)

    def on_ready(self, parts):
        self.data["state"] = READY_STATE

    def on_test(self, parts):
        # TODO: remove this
        # if self.data["state"] == READY_STATE:
            self.trigger_fight_start()

    def on_clock(self, parts):
        self.data["clk"] = parts[1][1:]
        self.clk_default = parts[1][1:]

        if self.data["state"] not in [FIGHT_STATE, INIT_STATE]:
            self.data["state"] = FIGHT_STATE
            self.trigger_fight_start()
            self.on_stable_update(self.data)
            self.on_event_trigger("START_ROUND")
        
        self.on_clock_update(self.data["clk"])

    def on_scoreboard(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        self.data["blue_points"][idx]["points"] = parts[1]
        self.data["red_points"][idx]["points"] = parts[3]
        if self.data["state"] in [FIGHT_STATE, BREAK_STATE]:
            self.on_stable_update(self.data)

    def on_penalty(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        self.data["blue_points"][idx]["penalties"] = parts[1]
        self.data["red_points"][idx]["penalties"] = parts[3]
        if self.data["state"] in [FIGHT_STATE, BREAK_STATE]:
            self.on_stable_update(self.data)

    def on_blue_hit(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        self.data["blue_points"][idx]["hits"] += 1

    def on_red_hit(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        self.data["red_points"][idx]["hits"] += 1

    def on_blue_scores(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        p_type = parts[1]
        if p_type == "1": self.data["blue_points"][idx]["punch"] += 1
        elif p_type in ["2", "4"]: self.data["blue_points"][idx]["trunk"] += 1
        elif p_type in ["3", "6"]: self.data["blue_points"][idx]["head"] += 1
        if p_type == "4": self.data["blue_points"][idx]["rotation_trunk"] += 1
        elif p_type == "6": self.data["blue_points"][idx]["rotation_head"] += 1
        self.on_event_trigger("BLUE_SCORE")

    def on_red_scores(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        p_type = parts[1]
        if p_type == "1": self.data["red_points"][idx]["punch"] += 1
        elif p_type in ["2", "4"]: self.data["red_points"][idx]["trunk"] += 1
        elif p_type in ["3", "6"]: self.data["red_points"][idx]["head"] += 1
        if p_type == "4": self.data["red_points"][idx]["rotation_trunk"] += 1
        elif p_type == "6": self.data["red_points"][idx]["rotation_head"] += 1
        self.on_event_trigger("RED_SCORE")

    def on_break(self, parts):
        self.data["clk"] = parts[1][1:]
        if self.data["state"] == FIGHT_STATE:
            self.data["state"] = BREAK_STATE
            self.on_stable_update(self.data)
            self.on_event_trigger("START_BREAK")
        self.on_clock_update(self.data["clk"])

    def on_win(self, parts):
        self.data["win"] = parts[1].lower()
        self.on_stable_update(self.data)
        self.on_event_trigger("WIN")
        self.data["state"] = INIT_STATE

    # --- Helper Lifecycles ---
    def evaluate_match_init(self):
        self.complete_data_counter += 1
        if self.complete_data_counter == 2:
            self.on_stable_update(self.data)
            self.on_event_trigger("NEW_FIGHT")
            self.complete_data_counter = 0

    def trigger_fight_start(self):
        if not self.data["fight_started"]:
            self.data["fight_started"] = True
            print("here")
            self.on_event_trigger("START_FIGHT")

    def reset_data(self):
        template = lambda: {"points": 0, "hits": 0, "trunk": 0, "rotation_trunk": 0, "head": 0, "rotation_head": 0, "punch": 0, "penalties": 0}
        self.data = {
            "clk": "", "state": INIT_STATE, "id": 0, "title": "", "category": "", "hit_level": 0, "round": 1, "win": "", "fight_started": False,
            "blue_name": "", "blue_flag2": "", "blue_flag3": "", "blue_points": [template(), template(), template()],
            "red_name": "", "red_flag2": "", "red_flag3": "", "red_points": [template(), template(), template()],
        }