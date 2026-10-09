from __future__ import annotations
from enum import Enum
from typing import List, Literal, Any, Optional, Union
from pydantic import BaseModel, Field, model_validator
from pydantic_step2 import ExtractionResult

class IssueTypeEnum(str, Enum):
    hallucination = 'hallucination'
    logic_conflict = 'logic_conflict'
    value_error = 'value_error'
    unit_mismatch = 'unit_mismatch'
    missing_data = 'missing_data'
    exactness_violation = 'exactness_violation'
    src_id_error = 'src_id_error'
    format_error = 'format_error'

class PatchAction(BaseModel):
    action_type: Literal['MODIFY', 'DELETE', 'ADD_ITEM'] = Field(..., description='The type of operation. MODIFY: Change value/key; DELETE: Remove object; ADD_ITEM: Append to list.')
    target_id: Optional[str] = Field(None, description='The unique `_id` of the Experiment or DataPoint to patch. REQUIRED for targeting data rows.')
    target_field: Optional[str] = Field(None, description="The specific field name (dot-notation) within the target object (e.g., 'conditions.pH.v'). REQUIRED for MODIFY/ADD_ITEM on IDs.")
    target_path: Optional[str] = Field(None, description="Dot-notation path (e.g., 'metadata.version') used ONLY for root-level singleton fields without IDs.")
    original_value_context: Optional[Union[str, int, float, bool, dict, list, None]] = Field(None, description='The value currently present in the Draft. MUST match exactly for safety verification. Set to null for ADD_ITEM or DELETE.')
    new_value: Any = Field(None, description='The new value to set (for MODIFY) or the complete JSON object to append (for ADD_ITEM). Ignored for DELETE.')
    issue_type: IssueTypeEnum = Field(..., description='The category of the error. This determines the weighting in the WESI calculation.')
    reason: str = Field(..., description="Evidence citing Source Text IDs (e.g., 'Table 1 at [ID:12] shows 5.5').")

    @model_validator(mode='after')
    def validate_targeting(self):
        if not self.target_id and (not self.target_path):
            raise ValueError("Invalid Patch: Must provide either 'target_id' (preferred) or 'target_path'.")
        if self.target_path:
            segments = self.target_path.split('.')
            if any((not seg.strip() for seg in segments)):
                raise ValueError("Invalid Patch: 'target_path' contains empty segments.")
            if any((seg.isdigit() for seg in segments)):
                raise ValueError("Invalid Patch: numeric indices are forbidden in 'target_path'. Use 'target_id'.")
        if self.target_id and self.action_type in ('MODIFY', 'ADD_ITEM') and (not self.target_field):
            raise ValueError("Invalid Patch: 'target_field' is required when using 'target_id' for MODIFY/ADD_ITEM.")
        return self

class AuditStats(BaseModel):
    quality_score: int = Field(..., description='0-100 confidence score. <60 indicates rejection.')
    critical_issues_count: int = Field(..., description='Count of severe errors (hallucinations, logic, value, unit, missing).')
    minor_issues_count: int = Field(..., description='Count of minor errors (format, src_id).')

class AuditReport(BaseModel):
    stats: AuditStats
    comments: str = Field(..., description='Executive summary of the extraction quality.')
    patches: List[PatchAction] = Field(default_factory=list, description='List of surgical corrections to apply.')
