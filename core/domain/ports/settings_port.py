from abc import ABC, abstractmethod

class SettingsPort(ABC):
    @abstractmethod
    def load_plugin_settings(self, plugin_name: str) -> dict:
        """Loads settings from storage (e.g., JSON file, Firebase)"""
        pass

    @abstractmethod
    def save_plugin_settings(self, plugin_name: str, settings_dict: dict) -> None:
        """Saves settings to storage"""
        pass