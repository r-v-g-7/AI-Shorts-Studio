from enum import Enum
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field


class QCCheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class CheckItem(BaseModel):
    name: str
    status: QCCheckStatus
    is_critical: bool = True
    message: str
    metric_value: Optional[Any] = None


class QualityReport(BaseModel):
    job_id: str
    overall_status: QCCheckStatus
    is_production_ready: bool
    total_checks: int
    passed_checks: int
    failed_checks: int
    warning_checks: int
    checks: List[CheckItem]
    generated_at: str
    video_path: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)
