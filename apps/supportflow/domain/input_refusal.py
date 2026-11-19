"""A refusal is not a business answer or a grant proposal."""
from typing import Annotated, Literal
from pydantic import Field
from .contracts import StrictModel, EvidenceRef


def enabled(snapshot):
    value = snapshot.get('input_refusal_policy_version')
    return type(value) is int and value == 1


class InputRefusalResult(StrictModel):
    schema_version: Literal[9] = 9
    resolution_code: Literal['INPUT_DECLINED'] = 'INPUT_DECLINED'
    customer: None = None
    evidence_refs: Annotated[list[EvidenceRef], Field(min_length=1, max_length=1)]
    observation_refs: Annotated[list, Field(max_length=0)] = []
    action_receipts: Annotated[list, Field(max_length=0)] = []
    reply_text: str = '你已明确拒绝补充必要信息，本次转人工处理，不再猜测客户。临时席位政策要求明确申请并确认客户、邀请邮箱、席位与天数；信息未确认，不能创建提案。本次没有授予席位。'
