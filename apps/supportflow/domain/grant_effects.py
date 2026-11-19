"""Public effect facts. A transport outcome is never evidence of no remote write."""
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from .contracts import Customer, EvidenceRef, Identifier, StrictModel, Subscription
from .grant_commands import InternalGrantReceipt


GRANT_REPLY_ACKNOWLEDGEMENT = "收到。"
GRANT_REPLY_PUBLIC_OPENING = "已核对本次申请的处理结果。以下列出经核验的临时席位、有效期限和回执编号，供您后续核对。"
PUBLIC_GRANT_REPLY_CONTRACT_VERSION = 1


def streamed_grant_reply_enabled(config: dict) -> bool:
    version = config.get("public_reply_contract_version")
    return (config.get("grant_reply_mode") == "streamed" and type(version) is int
            and version == PUBLIC_GRANT_REPLY_CONTRACT_VERSION)


class GrantVerification(StrictModel):
    status: Literal['verified_current', 'verified_historical', 'mismatch', 'pending']
    subscription: Subscription | None
    observed_at: AwareDatetime | None

    @model_validator(mode='after')
    def observation_is_bound(self):
        if (self.subscription is None) != (self.observed_at is None):
            raise ValueError('A subscription observation needs its processing timestamp')
        if self.status != 'pending' and self.subscription is None:
            raise ValueError('A completed verification needs observed facts')
        return self


class GrantExecutionResult(StrictModel):
    schema_version: Literal[4] = 4
    resolution_code: Annotated[str, Field(min_length=1, max_length=80, pattern=r'^[A-Z][A-Z0-9_]*$')]
    customer: Customer | None
    effect_status: Literal['none', 'unknown', 'committed']
    action_id: Identifier
    action_receipts: Annotated[list[InternalGrantReceipt], Field(max_length=1)]
    evidence_refs: Annotated[list[EvidenceRef], Field(max_length=5)]
    verification: GrantVerification | None
    reply_text: Annotated[str, Field(min_length=1, max_length=12000)]

    @model_validator(mode='after')
    def effect_is_bound(self):
        if (self.effect_status == 'committed') != (len(self.action_receipts) == 1):
            raise ValueError('Only a committed effect has exactly one receipt')
        if self.action_receipts:
            receipt = self.action_receipts[0]
            if receipt.action_id != self.action_id:
                raise ValueError('Receipt must belong to the original action')
            if self.customer is not None and receipt.customer_id != self.customer.customer_id:
                raise ValueError('Receipt customer must match the projected customer')
            if self.verification and self.verification.subscription is not None:
                if self.verification.subscription.subscription_id != receipt.subscription_id:
                    raise ValueError('Verification must observe the receipt subscription')
        if self.verification and self.verification.status != 'pending' and self.effect_status != 'committed':
            raise ValueError('Verification cannot establish an effect without a receipt')
        if '\0' in self.reply_text:
            raise ValueError('Reply must not contain NUL')
        return self
