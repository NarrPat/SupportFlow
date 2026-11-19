"""Decide a server proposal atomically; this HTTP boundary never calls a grant."""
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import ActorView, Db, ReadActor, check_csrf, check_request, live_actor, unauthorized
from .clarifications import event
from .objects import Identifier, Page, PageQuery, lock_customer_scope, not_found, principal
from .runs import RequestKey, RunStatus, resolve_request_key
from .runtime import ApiError
from apps.supportflow.domain.approvals import approval_capable_config, canonical_decision
from apps.supportflow.domain.clock import business_now
from apps.supportflow.domain.proposals import ProposalView, public_proposal
from apps.supportflow.jobs.waiting import waiting_milliseconds

router=APIRouter()
ApprovalStatus=Literal['pending','approved','rejected','expired','cancelled']


async def approval_actor(request:Request,db:Db):
    # Keep logout serialized by the session lock. Membership is reread and
    # locked after limits/run, matching the worker's order without preholding
    # membership while waiting for limits.
    actor=await live_actor(request,db,writing=True,lock_membership=False)
    if actor is None: raise unauthorized()
    check_request(request)
    check_csrf(request,'session-csrf',request.cookies[request.app.state.cookies.session_name])
    return actor


ApprovalWriteActor=Annotated[ActorView,Depends(approval_actor)]


async def locked_decider(db,actor):
    row=await (await db.execute('SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s FOR SHARE',
                                (actor.tenant_id,actor.actor_id))).fetchone()
    if row is None: raise unauthorized()
    current=actor.model_copy(update={'role':row['role']})
    require_supervisor(current)
    return current


class DecisionSubmit(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True)
    decision: Literal['approve','reject']
    reason: str = Field(min_length=0,max_length=1000)
    expected_version: int = Field(ge=1,le=2**31-1)
    client_request_id: RequestKey | None = None

    @field_validator('reason')
    @classmethod
    def reason_text(cls,value):
        if '\0' in value: raise ValueError('NUL is not allowed')
        value.encode('utf-8')
        return value


class ApprovalDecisionResult(BaseModel):
    approval_id: str
    run_id: str
    status: RunStatus
    approval_status: Literal['approved','rejected']
    version: int
    decision: Literal['approve','reject']
    reason: str
    decided_by: str
    decided_at: datetime


class ApprovalView(BaseModel):
    approval_id: str
    run_id: str
    status: ApprovalStatus
    version: int
    decision: Literal['approve','reject'] | None
    reason: str | None
    decided_by: str | None
    decided_at: datetime | None
    run_status: RunStatus
    can_decide: bool
    proposal: ProposalView

    @field_validator('proposal',mode='before')
    @classmethod
    def proposal_json(cls,value):
        import json
        return ProposalView.model_validate_json(json.dumps(value)) if isinstance(value,dict) else value


class ApprovalQuery(PageQuery):
    status: ApprovalStatus | None = None


# An approval grants scoped review of a proposal, never another actor's private
# thread or message. Current confirmed customer and ticket binding must agree.
APPROVAL_SCOPE="""a.tenant_id=%(tenant)s
    AND COALESCE(r.confirmed_fields->>'customer_id',t.customer_id)=p.canonical_args->>'customer_id'
    AND (t.customer_id IS NULL OR t.customer_id=p.canonical_args->>'customer_id')
    AND EXISTS(SELECT 1 FROM customer_scopes s WHERE s.tenant_id=a.tenant_id
        AND s.user_id=%(actor)s AND s.customer_id=p.canonical_args->>'customer_id')"""
APPROVAL_FROM="""FROM approvals a JOIN proposals p USING(tenant_id,run_id,proposal_id)
    JOIN runs r USING(tenant_id,run_id)
    JOIN threads h USING(tenant_id,thread_id,actor_id)
    JOIN tickets t USING(tenant_id,ticket_id)"""


def require_supervisor(actor):
    if actor.role!='supervisor':
        raise ApiError(403,'FORBIDDEN','只有主管可以审批。')


async def authorized_approval(db,actor,approval_id):
    row=await (await db.execute("""SELECT a.approval_id,r.run_id,r.actor_id AS requested_by,
        p.canonical_args->>'customer_id' AS customer_id """+APPROVAL_FROM+
        " WHERE "+APPROVAL_SCOPE+" AND a.approval_id=%(approval)s",
        {**principal(actor),'approval':approval_id})).fetchone()
    if row is None: raise not_found()
    return row


async def approval_comparison_now(db,*,run_id,approval_id,observed_processing_now,observed_business_now):
    """Only an isolated test bootstrap can replace comparison clocks, not elapsed wait."""
    return observed_business_now,observed_processing_now


async def view_approval(db,actor,approval_id):
    locator=await authorized_approval(db,actor,approval_id)
    require_supervisor(actor)
    await lock_customer_scope(db,actor,locator['customer_id'])
    row=await (await db.execute("""SELECT p.*,a.approval_id,a.status AS approval_status,
        a.version AS approval_version,a.decision,a.reason,a.decided_by,a.decided_at,
        r.status AS run_status,r.config_snapshot,r.actor_id AS requested_by,r.wait_expires_at,
        w.checkpoint_id AS wait_checkpoint_id,w.status AS wait_status """+APPROVAL_FROM+
        " LEFT JOIN proposal_waits w USING(tenant_id,run_id,proposal_id) WHERE "+APPROVAL_SCOPE+
        " AND a.approval_id=%(approval)s",{**principal(actor),'approval':approval_id})).fetchone()
    if row is None: raise not_found()
    from .documents import mask_run_evidence
    await mask_run_evidence(db,actor,{'result':{'schema_version':3,'evidence_refs':row['evidence_refs']}})
    now=(await (await db.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
    eligible=(row['approval_status']=='pending' and row['run_status']=='awaiting_approval'
              and row['requested_by']!=actor.actor_id and approval_capable_config(row['config_snapshot'])
              and row['wait_checkpoint_id'] is not None and row['wait_status']=='pending'
              and row['wait_expires_at'] is not None and now<row['wait_expires_at']
              and row['created_at']<=business_now(real_now=now)<row['expires_at'])
    return {'approval_id':row['approval_id'],'run_id':row['run_id'],'status':row['approval_status'],
            'version':row['approval_version'],'decision':row['decision'],'reason':row['reason'],
            'decided_by':row['decided_by'],'decided_at':row['decided_at'],'run_status':row['run_status'],
            'can_decide':bool(eligible),'proposal':public_proposal(row)}


@router.get('/approvals',response_model=Page[ApprovalView])
async def list_approvals(actor:ReadActor,db:Db,query:Annotated[ApprovalQuery,Query()]):
    require_supervisor(actor)
    rows=await (await db.execute('SELECT a.approval_id '+APPROVAL_FROM+' WHERE '+APPROVAL_SCOPE+
        " AND (%(status)s::text IS NULL OR a.status=%(status)s) ORDER BY p.created_at DESC,a.approval_id LIMIT %(limit)s OFFSET %(offset)s",
        {**principal(actor),'status':query.status,'limit':query.limit+1,'offset':query.offset})).fetchall()
    return {'items':[await view_approval(db,actor,row['approval_id']) for row in rows[:query.limit]],
            'limit':query.limit,'offset':query.offset,'has_more':len(rows)>query.limit}


@router.get('/approvals/{approval_id}',response_model=ApprovalView)
async def get_approval(approval_id:Identifier,actor:ReadActor,db:Db):
    return await view_approval(db,actor,approval_id)


async def record_key(db,actor,key,digest,approval):
    await db.execute("""INSERT INTO approval_decision_requests
        (tenant_id,actor_id,client_request_id,request_hash,run_id,approval_id,decision,approval_version,accepted_generation)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (actor.tenant_id,actor.actor_id,key,digest,approval['run_id'],approval['approval_id'],
         approval['decision'],approval['version'],approval['resume_generation']))


def decision_result(run,approval):
    return {'approval_id':approval['approval_id'],'run_id':run['run_id'],'status':run['status'],
            'approval_status':approval['status'],'version':approval['version'],'decision':approval['decision'],
            'reason':approval['reason'],'decided_by':approval['decided_by'],'decided_at':approval['decided_at']}


async def end_wait(db,run,approval,status,now):
    wait=await (await db.execute("SELECT * FROM proposal_waits WHERE proposal_id=%s AND status='pending' FOR UPDATE",
                                 (approval['proposal_id'],))).fetchone()
    elapsed=0
    if wait is not None:
        end=max(wait['wait_started_at'],now)
        elapsed=waiting_milliseconds(wait['wait_started_at'],end)
        await db.execute('UPDATE proposal_waits SET status=%s,wait_ended_at=%s WHERE proposal_id=%s',
                         (status,end,wait['proposal_id']))
    return elapsed


async def stop_pending(db,run,approval,*,status,reason,now):
    reference_status='expired' if status=='expired' else 'cancelled'
    elapsed=await end_wait(db,run,approval,reference_status,now)
    await db.execute('UPDATE approvals SET status=%s,version=version+1 WHERE approval_id=%s',
                     (reference_status,approval['approval_id']))
    await db.execute("UPDATE actions SET status=%s WHERE proposal_id=%s AND status='proposed'",
                     (reference_status,approval['proposal_id']))
    updated=await (await db.execute("""UPDATE runs SET status=%s,reason_code=%s,result=NULL,
        waiting_since=NULL,wait_expires_at=NULL,accumulated_wait_ms=accumulated_wait_ms+%s,
        version=version+1,last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *""",
        (status,reason,elapsed,run['run_id']))).fetchone()
    await event(db,updated,'run.status_changed',{'status':status,'reason_code':reason})


@router.post('/approvals/{approval_id}/decisions',status_code=202,response_model=ApprovalDecisionResult,
             responses={200:{'model':ApprovalDecisionResult}})
async def decide_approval(approval_id:Identifier,body:DecisionSubmit,request:Request,response:Response,
                          actor:ApprovalWriteActor,db:Db):
    from .main import error_response
    key=resolve_request_key(body,request)
    _,digest=canonical_decision(approval_id,body.decision,body.reason,body.expected_version)
    await db.execute("SET LOCAL lock_timeout='5s'")
    await db.execute("SET LOCAL statement_timeout='15s'")
    locator=await authorized_approval(db,actor,approval_id)
    require_supervisor(actor)
    if locator['requested_by']==actor.actor_id: raise ApiError(403,'FORBIDDEN','不能审批自己发起的申请。')
    limits=await (await db.execute("SELECT logical_queued FROM runtime_limits WHERE app='supportflow' FOR UPDATE")).fetchone()
    if limits is None: raise ApiError(503,'DEPENDENCY_UNAVAILABLE','运行准入尚未就绪。')
    run=await (await db.execute('SELECT * FROM runs WHERE tenant_id=%s AND run_id=%s FOR UPDATE',
                               (actor.tenant_id,locator['run_id']))).fetchone()
    actor=await locked_decider(db,actor)
    locator=await authorized_approval(db,actor,approval_id)
    if locator['requested_by']==actor.actor_id: raise ApiError(403,'FORBIDDEN','不能审批自己发起的申请。')
    await lock_customer_scope(db,actor,locator['customer_id'])
    approval=await (await db.execute('SELECT * FROM approvals WHERE tenant_id=%s AND approval_id=%s FOR UPDATE',
                                    (actor.tenant_id,approval_id))).fetchone()
    previous=await (await db.execute("""SELECT request_hash FROM approval_decision_requests
        WHERE app='supportflow' AND tenant_id=%s AND actor_id=%s AND operation='decide_approval'
          AND client_request_id=%s""",(actor.tenant_id,actor.actor_id,key))).fetchone()
    if previous is not None:
        if previous['request_hash']!=digest: raise ApiError(409,'IDEMPOTENCY_CONFLICT','此请求键已用于其他请求。')
        response.status_code=202 if approval['decision']=='approve' else 200
        return decision_result(run,approval)
    if approval['status'] in ('approved','rejected'):
        if approval['decision']!=body.decision: raise ApiError(409,'APPROVAL_CONFLICT','审批已经作出相反决定。')
        await record_key(db,actor,key,digest,approval)
        response.status_code=202 if body.decision=='approve' else 200
        return decision_result(run,approval)
    if (approval['status']!='pending' or run['status']!='awaiting_approval' or run['cancel_requested']
            or approval['version']!=body.expected_version):
        raise ApiError(409,'APPROVAL_CONFLICT','审批状态或版本已变化。')
    proposal=await (await db.execute('SELECT * FROM proposals WHERE proposal_id=%s',(approval['proposal_id'],))).fetchone()
    public_proposal({**proposal,'approval_id':approval_id})
    wait=await (await db.execute("SELECT * FROM proposal_waits WHERE proposal_id=%s AND status='pending' FOR UPDATE",
                                 (approval['proposal_id'],))).fetchone()
    if wait is None or not wait['checkpoint_id'] or run['wait_expires_at'] is None:
        raise ApiError(409,'APPROVAL_CONFLICT','审批等待尚未持久就绪。')
    now=(await (await db.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
    if not approval_capable_config(run['config_snapshot']):
        await stop_pending(db,run,approval,status='handoff',reason='CONFIG_VERSION_UNSUPPORTED',now=now)
        return error_response(request,ApiError(409,'CONFIG_VERSION_UNSUPPORTED','当前运行不支持审批恢复，请重新发起。'))
    business,processing=await approval_comparison_now(db,run_id=run['run_id'],approval_id=approval_id,
        observed_processing_now=now,observed_business_now=business_now(real_now=now))
    if not proposal['created_at']<=business<proposal['expires_at'] or processing>=run['wait_expires_at']:
        await stop_pending(db,run,approval,status='expired',reason='PROPOSAL_EXPIRED',now=now)
        return error_response(request,ApiError(409,'APPROVAL_EXPIRED','此提案已过期。'))
    if body.decision=='approve' and limits['logical_queued']>=10:
        raise ApiError(429,'QUEUE_FULL','待执行队列已满，请稍后重试。')
    approved=body.decision=='approve'
    generation=run['execution_generation']+1 if approved else None
    status='approved' if approved else 'rejected'
    elapsed=await end_wait(db,run,approval,status,now)
    approval=await (await db.execute("""UPDATE approvals SET status=%s,version=version+1,
        decision=%s,reason=%s,decided_by=%s,decided_at=%s,resume_generation=%s
        WHERE approval_id=%s RETURNING *""",
        (status,body.decision,body.reason,actor.actor_id,now,generation,approval_id))).fetchone()
    if approved:
        await db.execute("UPDATE runtime_limits SET logical_queued=logical_queued+1 WHERE app='supportflow'")
    else:
        await db.execute("UPDATE actions SET status='rejected' WHERE proposal_id=%s AND status='proposed'",(approval['proposal_id'],))
    run_status='resume_queued' if approved else 'rejected'
    reason='APPROVAL_ACCEPTED' if approved else 'APPROVAL_REJECTED'
    updated=await (await db.execute("""UPDATE runs SET status=%s,reason_code=%s,result=NULL,
        admission_state=%s,execution_generation=%s,version=version+1,last_event_seq=last_event_seq+1,
        waiting_since=NULL,wait_expires_at=NULL,accumulated_wait_ms=accumulated_wait_ms+%s
        WHERE run_id=%s RETURNING *""",(run_status,reason,'queued' if approved else 'none',
        generation if approved else run['execution_generation'],elapsed,run['run_id']))).fetchone()
    await record_key(db,actor,key,digest,approval)
    if approved:
        await db.execute("INSERT INTO outbox(tenant_id,run_id,execution_generation,kind) VALUES(%s,%s,%s,'resume')",
                         (actor.tenant_id,run['run_id'],generation))
    await event(db,updated,'approval.decided',{'approval_id':approval_id,'proposal_id':approval['proposal_id'],
        'action_id':proposal['action_id'],'decision':body.decision,'version':approval['version']})
    updated=await (await db.execute('UPDATE runs SET last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *',
                                   (run['run_id'],))).fetchone()
    await event(db,updated,'run.status_changed',{'status':run_status,'reason_code':reason})
    response.status_code=202 if approved else 200
    return decision_result(updated,approval)
