"""Read-only comparison of ordinary Compose topology, without starting services."""
import json
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
DEPLOY=ROOT/'deploy/supportflow'
BASE=('compose','compose.web','compose.worker','compose.diagnosis','compose.rag','compose.proposal',
      'compose.approval','compose.execution')


def config(mode):
    files=(*BASE,'compose.spring-java' if mode=='java' else 'compose.demodata')
    name='sf-java-workspace' if mode=='java' else 'sf-local'
    args=['docker','compose','--project-directory',str(DEPLOY),'-p',name]
    args.extend(item for name in files for item in ('-f',str(DEPLOY/(name+'.yaml'))))
    args+=['config','--format','json']
    env={**os.environ,'SF_JAVA_SECRETS_DIR':(ROOT/'.local/spring-workspace-secrets').as_posix()}
    return json.loads(subprocess.check_output(args,cwd=ROOT,env=env,timeout=20))


def main():
    java,python=config('java'),config('python')
    j,p=java['services'],python['services']
    checks={
      'python_default_still_python':p['sandbox']['image']=='sf-local-python-dev:env' and 'java-postgres' not in p,
      'java_is_direct_sandbox':j['sandbox']['image']=='sf-java-sandbox:workspace' and 'java-sandbox' not in j,
      'java_gets_only_runtime_credential':{s['source'] for s in j['sandbox']['secrets']}=={'java_runtime_password','sandbox_service_token'},
      'separate_private_business_db':set(j['java-postgres']['networks'])=={'java-business'}
          and java['networks']['java-business']['internal'] and 'java-business' not in j['worker']['networks'],
      'worker_url_unchanged':j['worker']['environment']['SF_SANDBOX_BASE_URL']==p['worker']['environment']['SF_SANDBOX_BASE_URL']=='http://sandbox:8001',
      'only_web_has_host_port':all({name for name,svc in x.items() if svc.get('ports')}=={'web'} for x in (j,p)),
      'ordinary_profiles_do_not_load_faults':all('spring-collector' not in x and all(
          svc.get('environment',{}).get('SF_RUNTIME_PROFILE')!='eval' and not any(
              v.get('target') in {'/private','/evidence','/work/tests/supportflow'} for v in svc.get('volumes',[]))
          for svc in x.values()) for x in (j,p)),
    }
    print(json.dumps({'status':'passed' if all(checks.values()) else 'failed','checks':checks},indent=2))
    return int(not all(checks.values()))


if __name__=='__main__':
    raise SystemExit(main())
