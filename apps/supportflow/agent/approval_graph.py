"""Explicit approval-capable graph; legacy proposal snapshots are never upgraded."""
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from apps.supportflow.domain.approvals import approval_compatible_config
from apps.supportflow.jobs import approval_dispatch, proposal_waiting, waiting
from apps.supportflow.jobs.contracts import FragmentOutcome, StopFragment
from . import grant_graph

GRAPH_VERSION = 'grant-approval-v1'


def build_graph(conn, run, gate, model, saver):
    return grant_graph.build_graph(conn, run, gate, model, saver,
                                   approval_handler=approval_dispatch.resume_approved)


def projection(conn, run, graph, saver):
    saved = saver.get_tuple(grant_graph.config(run))
    if saved is None:
        return None
    values = saved.checkpoint['channel_values']
    if (values.get('graph_version') or values.get('__start__',{}).get('graph_version')) != GRAPH_VERSION:
        return FragmentOutcome('handoff','CONFIG_VERSION_UNSUPPORTED')
    if 'authorization_id' in values:
        proposal = proposal_waiting.prepared(conn, run, proposal_id=values.get('proposal_id'))
        if proposal is None:
            return FragmentOutcome('handoff','CHECKPOINT_INCOMPATIBLE')
        try:
            actual = approval_dispatch.existing_authorization(conn,run,proposal,
                authorization_id=values['authorization_id'])
            if actual is None:
                return FragmentOutcome('handoff','CHECKPOINT_INCOMPATIBLE')
        except StopFragment as error:
            return FragmentOutcome(error.status,error.reason_code)
        return FragmentOutcome('handoff','EXECUTION_NOT_IMPLEMENTED')
    try:
        return grant_graph.projection(conn,run,graph,saver,graph_version=GRAPH_VERSION,
                                      approval_resume=approval_dispatch.read_resume)
    except StopFragment as error:
        return FragmentOutcome(error.status,error.reason_code)


def checkpoint_probe(conn, run):
    if not approval_compatible_config(run['config_snapshot']):
        return FragmentOutcome('handoff','CONFIG_VERSION_UNSUPPORTED')
    saver = PostgresSaver(conn)
    return projection(conn,run,build_graph(conn,run,None,None,saver),saver)


def approval_command(conn, run, *, approval_id):
    """Server-only Command value; there is no HTTP resume-state endpoint."""
    return approval_dispatch.read_resume(conn,run,approval_id=approval_id)


def execute_fragment(conn, run, gate, *, model):
    gate.check('approval.start')
    if not approval_compatible_config(run['config_snapshot']):
        return FragmentOutcome('handoff','CONFIG_VERSION_UNSUPPORTED')
    saver = PostgresSaver(conn)
    graph = build_graph(conn,run,gate,model,saver)
    saved = saver.get_tuple(grant_graph.config(run))
    initial = None
    if saved is None:
        fields = grant_graph._confirmed(run)
        names = ('customer_id','invitation_email','seats','days') if fields.requested_action else ('customer_id',)
        initial = {'graph_version':GRAPH_VERSION, 'grant_requested':fields.requested_action is not None,
                   'required_fields':[name for name in names if getattr(fields,name) is None]}
    else:
        outcome = projection(conn,run,graph,saver)
        if outcome is not None:
            return outcome
        snapshot = graph.get_state(grant_graph.config(run))
        pending = [item.value for task in snapshot.tasks for item in task.interrupts]
        if pending:
            if len(pending) != 1 or not isinstance(pending[0],dict):
                raise StopFragment('CHECKPOINT_INCOMPATIBLE')
            value = pending[0]
            if value.get('kind') == 'clarification':
                answer = waiting.read_resume(conn,run,input_id=value['input_id'])
            elif value.get('kind') == 'approval':
                answer = approval_command(conn,run,approval_id=value['approval_id'])
            else:
                raise StopFragment('CHECKPOINT_INCOMPATIBLE')
            if answer is None:
                raise StopFragment('APPROVAL_RESUME_INVALID' if value.get('kind')=='approval' else 'CLARIFICATION_RESUME_INVALID')
            initial = Command(resume=answer)
    graph.invoke(initial,grant_graph.config(run),durability='sync')
    gate.check('approval.after')
    result = projection(conn,run,graph,saver)
    if result is None:
        raise StopFragment('CHECKPOINT_INCOMPATIBLE')
    return result
