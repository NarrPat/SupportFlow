"""Project published offline reports through the current session scope."""

from pathlib import Path
import stat
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Query, Request
from pydantic import AnyUrl, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints
from pydantic import UrlConstraints, field_validator, model_validator

from .auth import ReadActor
from .objects import Page, PageQuery, not_found
from .runtime import ApiError


router = APIRouter()
CATALOG_PATH = Path("/data/evaluation/reports.json")
MAX_CATALOG_BYTES = 512 * 1024

ReportId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Count = Annotated[int, Field(ge=0, le=2_147_483_647)]
ReportRole = Literal["support", "supervisor", "admin"]
ExperimentUrl = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], host_required=True, max_length=2048)]


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class EvaluationBaseline(ReportModel):
    arm: ShortText
    model_id: ShortText
    graph_version: ShortText
    source_revision: ShortText


class EvaluationMetric(ReportModel):
    label: ShortText
    numerator: Count | None
    denominator: Annotated[int, Field(ge=1, le=2_147_483_647)] | None

    @model_validator(mode="after")
    def require_complete_pair(self):
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("Measured counts require a numerator and a denominator")
        return self


class EvaluationFailure(ReportModel):
    kind: Literal["product", "infrastructure", "fixture", "oracle", "runner", "not_implemented"]
    count: Annotated[int, Field(ge=1, le=2_147_483_647)]


class EvaluationReportSummary(ReportModel):
    report_id: ReportId
    title: ShortText
    experiment_id: ShortText
    generated_at: AwareDatetime


class EvaluationReport(EvaluationReportSummary):
    scope: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    experiment_url: ExperimentUrl | None
    baseline: EvaluationBaseline | None
    metrics: list[EvaluationMetric] = Field(max_length=40)
    failure_classes: list[EvaluationFailure] = Field(max_length=6)

    @field_validator("experiment_url")
    @classmethod
    def validate_experiment_url(cls, value):
        if value is not None and any(part is not None for part in
                (value.username, value.password, value.query, value.fragment)):
            raise ValueError("Experiment links must be stable HTTPS locations")
        return value


class PublishedReport(EvaluationReport):
    tenant_id: ShortText
    allowed_roles: list[ReportRole] = Field(min_length=1, max_length=3)


class ReportCatalog(ReportModel):
    schema_version: Literal[1]
    reports: list[PublishedReport] = Field(max_length=200)

    @model_validator(mode="after")
    def require_unique_report_ids(self):
        identifiers = {(report.tenant_id, report.report_id) for report in self.reports}
        if len(identifiers) != len(self.reports):
            raise ValueError("Report IDs must be unique within a tenant")
        return self


def read_catalog() -> list[PublishedReport]:
    """Read one bounded, operator-published file independently of request inputs."""
    path = CATALOG_PATH
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CATALOG_BYTES:
            raise ValueError("Catalog must be a bounded regular file")
        if path.resolve(strict=True) != path.absolute():
            raise ValueError("Catalog path must be direct")
        with path.open("rb") as stream:
            content = stream.read(MAX_CATALOG_BYTES + 1)
        if len(content) > MAX_CATALOG_BYTES:
            raise ValueError("Catalog exceeds its size limit")
        return ReportCatalog.model_validate_json(content).reports
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "暂时无法读取评测报告。") from error


async def visible_reports(actor: ReadActor) -> list[PublishedReport]:
    reports = await anyio.to_thread.run_sync(read_catalog)
    return [report for report in reports
            if report.tenant_id == actor.tenant_id and actor.role in report.allowed_roles]


@router.get("/eval/reports", response_model=Page[EvaluationReportSummary])
async def list_reports(actor: ReadActor, query: Annotated[PageQuery, Query()]):
    reports = await visible_reports(actor)
    reports.sort(key=lambda report: report.report_id)
    reports.sort(key=lambda report: report.generated_at, reverse=True)
    page = reports[query.offset:query.offset + query.limit]
    return Page[EvaluationReportSummary](
        items=[EvaluationReportSummary.model_validate(
            report.model_dump(include=set(EvaluationReportSummary.model_fields))) for report in page],
        limit=query.limit,
        offset=query.offset,
        has_more=query.offset + query.limit < len(reports),
    )


@router.get("/eval/reports/{report_id}", response_model=EvaluationReport)
async def get_report(report_id: ReportId, actor: ReadActor, request: Request):
    if request.query_params:
        raise ApiError(422, "VALIDATION_ERROR", "请求字段无效。")
    for report in await visible_reports(actor):
        if report.report_id == report_id:
            return EvaluationReport.model_validate(report.model_dump(exclude={"tenant_id", "allowed_roles"}))
    raise not_found()
