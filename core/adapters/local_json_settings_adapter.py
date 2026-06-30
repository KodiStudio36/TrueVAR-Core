import json
import os
from core.domain.ports.settings_port import SettingsPort

class LocalJSONSettingsAdapter(SettingsPort):
    def __init__(self, storage_dir: str = "config/plugins"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)

    def load_plugin_settings(self, plugin_name: str) -> dict:
        filepath = os.path.join(self.storage_dir, f"{plugin_name}.json")
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                return json.load(f)
        return {} # Return empty if no settings exist yet

    def save_plugin_settings(self, plugin_name: str, settings_dict: dict) -> None:
        filepath = os.path.join(self.storage_dir, f"{plugin_name}.json")
        with open(filepath, "w") as f:
            json.dump(settings_dict, f, indent=4)