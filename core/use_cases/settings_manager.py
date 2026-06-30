from typing import Callable, Dict, Type, Any
from pydantic import BaseModel
from core.domain.ports.settings_port import SettingsPort

class SettingsManager:
    def __init__(self, adapter: SettingsPort):
        self.adapter = adapter
        self.schemas: Dict[str, Type[BaseModel]] = {}
        self.callbacks: Dict[str, Callable] = {}
        self.current_values: Dict[str, BaseModel] = {}

    def register_plugin(self, plugin_name: str, schema: Type[BaseModel], callback: Callable):
        """Called by a plugin on boot to register its settings."""
        self.schemas[plugin_name] = schema
        self.callbacks[plugin_name] = callback
        
        # 1. Load saved raw dictionary from storage
        saved_data = self.adapter.load_plugin_settings(plugin_name)
        
        # 2. Parse and validate through Pydantic. 
        # (If saved data is empty, Pydantic uses the class defaults!)
        validated_settings = schema(**saved_data)
        self.current_values[plugin_name] = validated_settings
        
        # 3. Save it back immediately to ensure defaults are written to disk
        self.adapter.save_plugin_settings(plugin_name, validated_settings.model_dump())

    def get_settings(self, plugin_name: str) -> BaseModel:
        return self.current_values.get(plugin_name)

    def get_schema(self, plugin_name: str) -> dict:
        """Returns the JSON schema to automatically build HTML frontends."""
        return self.schemas[plugin_name].model_json_schema()

    async def update_settings(self, plugin_name: str, new_data: dict):
        """Called by the API when a user submits a form."""
        schema = self.schemas[plugin_name]
        
        # 1. Validate new data
        updated_settings = schema(**new_data)
        self.current_values[plugin_name] = updated_settings
        
        # 2. Save to storage
        self.adapter.save_plugin_settings(plugin_name, updated_settings.model_dump())
        
        # 3. Trigger the plugin's restart/update callback dynamically!
        callback = self.callbacks.get(plugin_name)
        if callback:
            await callback(updated_settings)
            
        return updated_settings