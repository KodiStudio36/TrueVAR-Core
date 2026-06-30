from pydantic import BaseModel

class KyorugiListenerSettings(BaseModel):
    scoring_system_port: int = 5000