"""Explicit nine-round grant graph. Old approval snapshots remain read-only."""
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from apps.supportflow.domain.approvals import execution_compatible_config
from apps.supportflow.domain.grant_effects import streamed_grant_reply_enabled
from apps.supportflow.jobs import approval_dispatch, grant_effects, proposal_waiting, waiting
from apps.supportflow.jobs.contracts import FragmentOutcome, StopFragment
from apps.supportflow.jobs.public_text import PublicTextWriter
from . import grant_graph
from .grant_reply import present_verified_grant

GRAPH_VERSION = 'grant-execution-v1'


def execute_authorized(conn,run,gate,state):
    proposal = proposal_waiting.prepared(conn,run,proposal_id=state['proposal_id'])
    if proposal is None or not state.get('authorization_id'):
        raise StopFragment('CHECKPOINT_INCOMPATIBLE')
    row = grant_effects.send(conn,run,gate,proposal,state['authorization_id'])
    if row['status'] == 'committed':
        row = grant_effects.verify(conn,run,row,normal=True)
    outcome = grant_effects.effect_outcome(conn,run,row=row)
    return {'execution_outcome':{'status':outcome.status,'reason_code':outcome.reason_code,'result':outcome.result}}


def build_graph(conn,run,gate,model,saver):
    return grant_graph.build_graph(conn,run,gate,model,saver,
        approval_handler=approval_dispatch.resume_approved,execution_handler=execute_authorized)


def projection(conn,run,graph,saver):
    effect = grant_effects.effect_outcome(conn,run)
    if effect is not None:
        return effect
    return grant_graph.projection(conn,run,graph,saver,graph_version=GRAPH_VERSION,
                                   approval_resume=approval_dispatch.read_resume)


def checkpoint_probe(conn,run):
    if not execution_compatible_config(run['config_snapshot']):
        return FragmentOutcome('handoff','CONFIG_VERSION_UNSUPPORTED')
    saver = PostgresSaver(conn)
    return projection(conn,run,build_graph(conn,run,None,None,saver),saver)


def execute_fragment(conn,run,gate,*,model):
    if not execution_compatible_config(run['config_snapshot']):
        return FragmentOutcome('handoff','CONFIG_VERSION_UNSUPPORTED')
    # Check durable effect first. This branch never resumes the ordinary graph,
    # spends model attempts or reissues a command after a checkpoint gap.
    effect = grant_effects.effect_outcome(conn,run)
    if effect is not None:
        return effect
    gate.check('execution.start')
    saver = PostgresSaver(conn)
    graph = build_graph(conn,run,gate,model,saver)
    saved = saver.get_tuple(grant_graph.config(run))
    initial = None
    if saved is None:
        fields = grant_graph._confirmed(run)
        names = ('customer_id','invitation_email','seats','days') if fields.requested_action else ('customer_id',)
        initial = {'graph_version':GRAPH_VERSION,'grant_requested':fields.requested_action is not None,
                   'required_fields':[name for name in names if getattr(fields,name) is None]}
    else:
        outcome = projection(conn,run,graph,saver)
        if outcome is not None:
            return outcome
        pending = [item.value for task in graph.get_state(grant_graph.config(run)).tasks for item in task.interrupts]
        if pending:
            if len(pending) != 1 or not isinstance(pending[0],dict):
                raise StopFragment('CHECKPOINT_INCOMPATIBLE')
            value = pending[0]
            if value.get('kind') == 'clarification':
                answer = waiting.read_resume(conn,run,input_id=value['input_id'])
            elif value.get('kind') == 'approval':
                answer = approval_dispatch.read_resume(conn,run,approval_id=value['approval_id'])
            else:
                raise StopFragment('CHECKPOINT_INCOMPATIBLE')
            if answer is None:
                raise StopFragment('APPROVAL_RESUME_INVALID')
            initial = Command(resume=answer)
    graph.invoke(initial,grant_graph.config(run),durability='sync')
    result = projection(conn,run,graph,saver)
    if result is None:
        raise StopFragment('CHECKPOINT_INCOMPATIBLE')
    if streamed_grant_reply_enabled(run['config_snapshot']):
        writer = PublicTextWriter(conn, run['run_id'], run['execution_generation'])
        return present_verified_grant(result, run, gate, model, publish_delta=writer.delta)
    return present_verified_grant(result, run, gate, model)
