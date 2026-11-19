"""Publish only genuinely retrieved policy, revalidated in the closing transaction.

No network, model, embedding or queue is needed to accept a refusal. Missing or
stale evidence only suppresses the explanation; it never resumes a refused task.
"""
from .documents import get_citation, lock_visible_document
from .runtime import ApiError
from apps.supportflow.domain.input_refusal import InputRefusalResult, enabled
from apps.supportflow.domain.grant_policy import load_policy_bundle


async def policy_result(db, actor, run, request):
    if not enabled(run['config_snapshot']) or run['confirmed_fields'].get('requested_action') != 'grant_temporary_seats':
        return None
    member = await (await db.execute('SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s FOR SHARE',
        (actor.tenant_id, actor.actor_id))).fetchone()
    if not member or member['role'] not in {'support','supervisor','admin'}:
        return None
    state = await (await db.execute('SELECT active_generation FROM knowledge_state WHERE tenant_id=%s FOR SHARE',
        (actor.tenant_id,))).fetchone()
    if not state or state['active_generation'] is None:
        return None
    generation = state['active_generation']
    index = await (await db.execute('SELECT state,vector_ready,lexical_ready FROM index_generations WHERE tenant_id=%s AND generation=%s',
        (actor.tenant_id,generation))).fetchone()
    if not index or index['state'] != 'active' or not index['vector_ready'] or not index['lexical_ready']:
        return None
    rows = await (await db.execute('''SELECT refs FROM run_retrievals
        WHERE tenant_id=%s AND run_id=%s AND execution_generation<=%s
          AND index_generation=%s AND error_code IS NULL ORDER BY execution_generation DESC,ordinal DESC LIMIT 10''',
        (actor.tenant_id,run['run_id'],run['execution_generation'],generation))).fetchall()
    pin = load_policy_bundle()
    for row in rows:
        for ref in row['refs']:
            if ref['section_id'] != pin['section_id'] or ref['content_sha256'] != pin['content_sha256']:
                continue
            try:
                await lock_visible_document(db,actor,ref['document_id'])
                current = await (await db.execute('SELECT active_version_id FROM documents WHERE tenant_id=%s AND document_id=%s',
                    (actor.tenant_id,ref['document_id']))).fetchone()
                if not current or current['active_version_id'] != ref['version_id']:
                    return None
                citation = await get_citation(ref['evidence_id'],request,actor,db,generation=generation)
                if any(citation.get(k) != v for k,v in ref.items()):
                    return None
                return InputRefusalResult(evidence_refs=[ref]).model_dump(mode='json')
            except ApiError:
                return None
    return None
