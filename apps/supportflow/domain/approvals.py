"""Server-owned approval compatibility and request binding; no execution authority."""
import hashlib
import json


def approval_compatible_config(config):
    return (isinstance(config, dict) and config.get('contract_version') == 'dispatch-v1'
            and config.get('graph_version') == 'grant-approval-v1'
            and all(type(config.get(name)) is int and config[name] == value for name,value in
                    (('schema_version',1),('grant_contract_version',1),
                     ('approval_contract_version',1),('tool_round_limit',7))))


def execution_compatible_config(config):
    """New write graph only; old seven-round snapshots never gain write authority."""
    if not isinstance(config, dict):
        return False
    version = config.get('execution_contract_version')
    if type(version) is not int or version not in (1, 2):
        return False
    return (isinstance(config, dict) and config.get('contract_version') == 'dispatch-v1'
            and config.get('graph_version') == 'grant-execution-v1'
            and all(type(config.get(name)) is int and config[name] == value for name,value in
                    (('schema_version',1),('grant_contract_version',1),('approval_contract_version',1),
                     ('execution_contract_version',version),('tool_round_limit',9 if version == 1 else 10))))


def approval_capable_config(config):
    return approval_compatible_config(config) or execution_compatible_config(config)


def canonical_decision(approval_id, decision, reason, expected_version):
    if (not isinstance(approval_id,str) or not 1 <= len(approval_id) <= 128 or not approval_id.strip()
            or '\0' in approval_id or decision not in ('approve','reject') or not isinstance(decision,str)
            or not isinstance(reason,str) or len(reason)>1000 or '\0' in reason
            or type(expected_version) is not int or not 1 <= expected_version <= 2**31-1):
        raise ValueError('Invalid approval decision')
    payload={'schema_version':1,'operation':'decide_approval','approval_id':approval_id,
             'decision':decision,'reason':reason,'expected_version':expected_version}
    encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    return encoded,hashlib.sha256(encoded.encode('utf-8')).hexdigest()
