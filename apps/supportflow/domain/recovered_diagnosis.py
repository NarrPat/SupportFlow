"""Explicit result DTO for bounded, read-only replay; legacy refs stay strict."""
from typing import Annotated,Literal
from pydantic import Field,model_validator,field_validator
from .contracts import StrictModel,Identifier,RagDiagnosisResult

def enabled(snapshot):
    value=snapshot.get('parallel_read_recovery_contract_version')
    return type(value) is int and value==1

class RecoveryObservationRef(StrictModel):
    run_id: Identifier
    execution_generation: Annotated[int,Field(ge=1)]
    ordinal: Annotated[int,Field(ge=1,le=10)]
    tool_id: Literal['get_customer','get_subscription','get_invitation','get_service_status']

class ReadRecovery(StrictModel):
    contract_version: Literal[1]
    execution_generation: Annotated[int,Field(ge=2)]
    recovery_count: Annotated[int,Field(ge=1)]
    tool_rounds: Annotated[int,Field(ge=1,le=10)]

    @field_validator('contract_version',mode='before')
    @classmethod
    def integer_version(cls,value):
        if type(value) is not int:raise ValueError('Recovery contract version must be an integer')
        return value

class RecoveredRagDiagnosisResult(RagDiagnosisResult):
    schema_version: Literal[8]=8
    recovery: ReadRecovery
    observation_refs: Annotated[list[RecoveryObservationRef],Field(min_length=1,max_length=10)]

    @model_validator(mode='after')
    def recovery_boundary(self):
        refs=self.observation_refs
        keys={(r.run_id,r.execution_generation,r.ordinal) for r in refs}
        if len(keys)!=len(refs) or len({r.run_id for r in refs})!=1:
            raise ValueError('Recovery references require one run and unique identities')
        if any(r.execution_generation>self.recovery.execution_generation or r.ordinal>self.recovery.tool_rounds for r in refs):
            raise ValueError('Reference exceeds actual recovery generation or reservation')
        return self
