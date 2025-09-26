from typing import Dict, Any
from datetime import datetime

class Plugin:
    plugin_type = "transport"
    plugin_id = "test"

    async def initialize(self, config: Dict[str, Any]) -> None:
        return

    async def send_fax(self, to_number: str, file_path: str, **kwargs) -> Dict[str, Any]:
        return {
            "job_id": f"test-{int(datetime.utcnow().timestamp())}",
            "provider_sid": "test-sid",
            "status": "queued",
            "to_number": to_number,
            "provider": "test",
            "metadata": {"note": "dummy-transport"}
        }
