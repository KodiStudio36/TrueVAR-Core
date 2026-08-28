from datetime import datetime
import copy
import re

ALPHA3_TO_ALPHA2 = {
    "AFG": "af", "ALB": "al", "ALG": "dz", "AND": "ad", "ANG": "ao",
    "ARG": "ar", "ARM": "am", "AUS": "au", "AUT": "at", "AZE": "az",
    "BAH": "bs", "BAN": "bd", "BAR": "bb", "BEL": "be", "BEN": "bj",
    "BIH": "ba", "BIZ": "bz", "BLR": "by", "BOL": "bo", "BOT": "bw",
    "BRA": "br", "BRN": "bh", "BRU": "bn", "BUL": "bg", "BUR": "bf",
    "CAF": "cf", "CAM": "kh", "CAN": "ca", "CHI": "cl", "CHN": "cn",
    "CIV": "ci", "CMR": "cm", "COD": "cd", "CGO": "cg", "COL": "co",
    "CRC": "cr", "CRO": "hr", "CUB": "cu", "CYP": "cy", "CZE": "cz",
    "DEN": "dk", "DOM": "do", "ECU": "ec", "EGY": "eg", "ESA": "sv",
    "ESP": "es", "EST": "ee", "ETH": "et", "FIJ": "fj", "FIN": "fi",
    "FRA": "fr", "GAB": "ga", "GAM": "gm", "GBR": "gb", "GEO": "ge",
    "GER": "de", "GHA": "gh", "GRE": "gr", "GUA": "gt", "GUI": "gn",
    "HAI": "ht", "HKG": "hk", "HON": "hn", "HUN": "hu", "INA": "id",
    "IND": "in", "IRI": "ir", "IRQ": "iq", "IRL": "ie", "ISL": "is",
    "ISR": "il", "ITA": "it", "JAM": "jm", "JOR": "jo", "JPN": "jp",
    "KAZ": "kz", "KEN": "ke", "KGZ": "kg", "KOS": "xk", "KOR": "kr",
    "KSA": "sa", "KUW": "kw", "LAO": "la", "LAT": "lv", "LBN": "lb",
    "LBR": "lr", "LBY": "ly", "LIE": "li", "LTU": "lt", "LUX": "lu",
    "MAR": "ma", "MAS": "my", "MDG": "mg", "MDA": "md", "MEX": "mx",
    "MGL": "mn", "MKD": "mk", "MLI": "ml", "MLT": "mt", "MNE": "me",
    "MOZ": "mz", "MRI": "mu", "MTN": "mr", "MYA": "mm", "NAM": "na",
    "NED": "nl", "NEP": "np", "NGR": "ng", "NIC": "ni", "NOR": "no",
    "NZL": "nz", "OMA": "om", "PAK": "pk", "PAN": "pa", "PAR": "py",
    "PER": "pe", "PHI": "ph", "PNG": "pg", "POL": "pl", "POR": "pt",
    "PRK": "kp", "PUR": "pr", "QAT": "qa", "ROU": "ro", "RSA": "za",
    "RUS": "ru", "RWA": "rw", "SAM": "ws", "SEN": "sn", "SGP": "sg",
    "SKN": "kn", "SLE": "sl", "SLO": "si", "SMR": "sm", "SOL": "sb",
    "SOM": "so", "SRB": "rs", "SRI": "lk", "SUD": "sd", "SUI": "ch",
    "SUR": "sr", "SVK": "sk", "SWE": "se", "SWZ": "sz", "SYR": "sy",
    "TAN": "tz", "TJK": "tj", "TKM": "tm", "TLS": "tl", "TOG": "tg",
    "THA": "th", "TPE": "tw", "TTO": "tt", "TUN": "tn", "TUR": "tr",
    "UAE": "ae", "UGA": "ug", "UKR": "ua", "URU": "uy", "USA": "us",
    "UZB": "uz", "VAN": "vu", "VEN": "ve", "VIE": "vn", "YEM": "ye",
    "ZAM": "zm", "ZIM": "zw",
    # ISO-3 Fallback aliases (ISO 3166-1 vs IOC mismatches)
    "CHE": "ch", "DEU": "de", "DNK": "dk", "GRC": "gr", "NLD": "nl",
    "PRT": "pt", "SVN": "si", "ZAF": "za", "ROC": "ru", "AIN": "un"
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
            "ch1": self.on_ivr,
            "ch2": self.on_ivr,
            "brk": self.on_break,
            "ij0": self.on_doctor,
            "win": self.on_win,
            "wmh": self.on_win_data,
        }

    def parse_string(self, msg: str):
        parts = msg.strip().split(";")
        if not parts or not parts[0]:
            return
        command = parts[0].lower()
        handler = self.flags.get(command)
        if handler:
            handler(parts)

    def normalize_category(self, category_str: str, subcategory_str: str):
        # 1. Combine and clean the strings into a single, uniform uppercase string
        category = category_str.lower().strip() if category_str else ""
        subcategory = subcategory_str.lower().strip() if subcategory_str else ""
        combined = f"{category} {subcategory}"

        # 2. Identify Age Division
        division = "seniors" # Default fallback
        if "jun" in combined:
            division = "juniors"
        elif "youn" in combined:
            division = "youngercadets"
        elif "cad" in combined or "kad" in combined:
            division = "cadets"
        elif "pup" in combined or "chi" in combined:
            division = "pupils"

        # 3. Identify Gender
        gender = "men" # Default fallback
        if (
            "w" in category or 
            "f" in category or 
            "z" in category or
            "ž" in category
        ):
            gender = "women"

        # 4. Extract Weight Class
        weight_sign = "plus" if ("plus" in category or "+" in category) else "under"

        number_match = re.search(r'\d+', category)
        if number_match:
            weight_num = number_match.group(0)
        else:
            return f"{division}_{gender}_unknown"

        # 5. Construct the strictly formatted key
        return f"{division}_{gender}_{weight_sign}_{weight_num}kg"

    # --- Parser Action Methods ---
    def on_match(self, parts):
        if self.complete_data_counter == 0: 
            self.reset_data()

        self.data.update({
            "match_id": parts[1], 
            "title": parts[2], 
            "category": self.normalize_category(parts[3], parts[10]), 
            "hitLevel": int(parts[14]),
        })
        self.evaluate_match_init()

    def on_athletes(self, parts):
        if self.complete_data_counter == 0: 
            self.reset_data()
        self.data["blue"].update({
            "name": parts[1], "flag2": ALPHA3_TO_ALPHA2.get(parts[3], "un").lower(), "flag3": parts[3],
        })
        self.data["red"].update({
            "name": parts[5], "flag2": ALPHA3_TO_ALPHA2.get(parts[7], "un").lower(), "flag3": parts[7],
        })
        self.evaluate_match_init()

    def on_round(self, parts):
        new_round = int(parts[1])
        if self.data["round"] != new_round:
            self.data["round"] = new_round
            if self.data["state"] in [FIGHT_STATE, BREAK_STATE]:
                print(f"⏱️ [EVENT] Round updated -> Round {self.data['round']}")
                self.on_stable_update(self.data)

    def on_ready(self, parts):
        self.data["state"] = READY_STATE
        print(f"🚦 [STATE] Scoreboard ready -> Match #{self.data['match_id']}")

    def on_test(self, parts):
        if self.data["state"] == READY_STATE:
            self.trigger_fight_start()

    def on_clock(self, parts):
        self.data["clk"] = parts[1][1:] if len(parts[1]) == 5 else parts[1]

        if self.data["state"] not in [FIGHT_STATE, INIT_STATE]:
            self.data["state"] = FIGHT_STATE
            self.trigger_fight_start()
            print(f"🔔 [EVENT] START_ROUND -> Round {self.data['round']} (Clock: {self.data['clk']})")
            self.on_stable_update(self.data)
            self.on_event_trigger("START_ROUND")

        if self.data["doctor"]:
            self.data["doctor"] = False
            self.on_stable_update(self.data)
        
        self.on_clock_update(self.data["clk"])

    def on_scoreboard(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        self.data["blue"]["points"][idx]["points"] = parts[1]
        self.data["red"]["points"][idx]["points"] = parts[3]
        if self.data["state"] in [FIGHT_STATE, BREAK_STATE]:
            print(f"📊 [EVENT] Scoreboard Update (Round {self.data['round']}) -> 🟦 {parts[1]} : {parts[3]} 🟥")
            self.on_stable_update(self.data)

    def on_penalty(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        self.data["blue"]["points"][idx]["penalties"] = parts[1]
        self.data["red"]["points"][idx]["penalties"] = parts[3]
        if self.data["state"] in [FIGHT_STATE, BREAK_STATE]:
            print(f"⚠️ [EVENT] Penalty Update (Round {self.data['round']}) -> 🟦 Gam-jeom: {parts[1]} | 🟥 Gam-jeom: {parts[3]}")
            self.on_stable_update(self.data)

    def on_blue_hit(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        self.data["blue"]["points"][idx]["hits"] += 1
        print(f"🥊 [HIT] Blue registered hit threshold count: {self.data['blue']['points'][idx]['hits']}")

    def on_red_hit(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        self.data["red"]["points"][idx]["hits"] += 1
        print(f"🥊 [HIT] Red registered hit threshold count: {self.data['red']['points'][idx]['hits']}")

    def on_blue_scores(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        p_type = parts[1]
        if p_type == "1": self.data["blue"]["points"][idx]["punch"] += 1
        elif p_type in ["2", "4"]: self.data["blue"]["points"][idx]["trunk"] += 1
        elif p_type in ["3", "6"]: self.data["blue"]["points"][idx]["head"] += 1
        if p_type == "4": self.data["blue"]["points"][idx]["rotation_trunk"] += 1
        elif p_type == "6": self.data["blue"]["points"][idx]["rotation_head"] += 1
        
        print(f"🟦 [EVENT] BLUE_SCORE -> Type code: {p_type}")
        self.on_event_trigger("BLUE_SCORE", {"p_type": p_type})

    def on_red_scores(self, parts):
        idx = max(0, min(self.data["round"] - 1, 2))
        p_type = parts[1]
        if p_type == "1": self.data["red"]["points"][idx]["punch"] += 1
        elif p_type in ["2", "4"]: self.data["red"]["points"][idx]["trunk"] += 1
        elif p_type in ["3", "6"]: self.data["red"]["points"][idx]["head"] += 1
        if p_type == "4": self.data["red"]["points"][idx]["rotation_trunk"] += 1
        elif p_type == "6": self.data["red"]["points"][idx]["rotation_head"] += 1
        
        print(f"🟥 [EVENT] RED_SCORE -> Type code: {p_type}")
        self.on_event_trigger("RED_SCORE", {"p_type": p_type})

    def on_ivr(self, parts):
        if parts[1] == "":
            self.on_event_trigger("IVR")

    def on_break(self, parts):
        self.data["clk"] = parts[1][1:] if len(parts[1]) == 5 else parts[1]
        if self.data["state"] == FIGHT_STATE:
            self.data["state"] = BREAK_STATE
            print(f"⏸️ [EVENT] START_BREAK -> Round break started (Clock: {self.data['clk']})")
            self.on_stable_update(self.data)
            self.on_event_trigger("START_BREAK")
        self.on_clock_update(self.data["clk"])

    def on_doctor(self, parts):
        self.data["clk"] = parts[1][1:] if len(parts[1]) == 5 else parts[1]
        print(self.data["doctor"])
        if not self.data["doctor"]:
            self.data["doctor"] = True
            self.on_stable_update(self.data)

        self.on_clock_update(self.data["clk"])

    def on_win(self, parts):
        winner = parts[1].lower()
        self.data["win"] = winner
        print(f"🏆 [EVENT] WIN -> Winner: {winner.upper()}")
        self.on_stable_update(self.data)

    def on_win_data(self, parts):
        self.data["win_score"], self.data["win_type"] = parts[2].lower().split(" ")

        self.on_event_trigger("WIN", copy.deepcopy(self.data))
        self.data["state"] = INIT_STATE

    # --- Helper Lifecycles ---
    def evaluate_match_init(self):
        self.complete_data_counter += 1
        if self.complete_data_counter == 2:
            blue_name = self.data['blue']['name']
            red_name = self.data['red']['name']
            match_id = self.data['match_id']
            cat = self.data['category']
            
            print(f"🔥 [EVENT] NEW_FIGHT loaded -> Match #{match_id}: 🟦 {blue_name} vs 🟥 {red_name} ({cat})")
            self.on_stable_update(self.data)
            self.on_event_trigger("NEW_FIGHT", self.data)
            self.complete_data_counter = 0

    def trigger_fight_start(self):
        if not self.data["fight_started"]:
            self.data["fight_started"] = True
            self.data["start_time"] = datetime.now()
            print(f"🥋 [EVENT] START_FIGHT triggered at {self.data['start_time'].strftime('%H:%M:%S')}")
            self.on_event_trigger("START_FIGHT")

    def reset_data(self):
        clk = self.data.get("clk", "")
        template = lambda: {"points": 0, "hits": 0, "trunk": 0, "rotation_trunk": 0, "head": 0, "rotation_head": 0, "punch": 0, "penalties": 0}
        fighter = lambda: {"name": "", "flag2": "", "flag3": "", "points": [template(), template(), template()]}
        self.data = {
            "clk": clk, "state": INIT_STATE, "match_id": 0, "title": "", "category": "", "hitLevel": 0, "round": 1, "win": "", "win_score": "", 
            "win_type": "", "fight_started": False, "start_time": None, "doctor": False,
            "blue": fighter(),
            "red": fighter(),
        }